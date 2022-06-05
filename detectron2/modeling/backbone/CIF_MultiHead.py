import torch.nn as nn
import torch
import numpy as np
from .CIF import build_cif_backbone
from .backbone import Backbone
from .build import BACKBONE_REGISTRY

from detectron2.layers import ShapeSpec

__all__=["build_cif_multihead_backbone"]

class ScaledDotProductAttention(nn.Module):
    def __init__(self):
        super(ScaledDotProductAttention, self).__init__()

    def forward(self, Q, K, V,d_k):
        '''
        Q: [batch_size, n_heads, len_q, d_k]
        K: [batch_size, n_heads, len_k, d_k]
        V: [batch_size, n_heads, len_v(=len_k), d_v]
        attn_mask: [batch_size, n_heads, seq_len, seq_len]
        '''
        scores = torch.matmul(Q, K.transpose(-1, -2)) / np.sqrt(d_k) # scores : [batch_size, n_heads, len_q, len_k]        
        attn = nn.Softmax(dim=-1)(scores)
        context = torch.matmul(attn, V) # [batch_size, n_heads, len_q, d_v]
        return context

class MultiHeadAttention(nn.Module):
    def __init__(self,d_model=512,d_k=64,d_v=64,n_heads=8):
        super(MultiHeadAttention, self).__init__()
        self.d_k = d_k
        self.d_v = d_v
        self.n_heads = n_heads
        self.W_Q = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_K = nn.Linear(d_model, d_k * n_heads, bias=False)
        self.W_V = nn.Linear(d_model, d_v * n_heads, bias=False)
        self.fc = nn.Linear(n_heads * d_v, d_model, bias=False)
        self.norm = nn.LayerNorm(d_model)
    def forward(self, input_Q, input_K, input_V):
        '''
        input_Q: [batch_size, len_q, d_model]
        input_K: [batch_size, len_k, d_model]
        input_V: [batch_size, len_v(=len_k), d_model]
        attn_mask: [batch_size, seq_len, seq_len]
        '''
        residual, batch_size = input_Q, input_Q.size(0)
        # (B, S, D) -proj-> (B, S, D_new) -split-> (B, S, H, W) -trans-> (B, H, S, W)
        Q = self.W_Q(input_Q).view(batch_size, -1, self.n_heads, self.d_k).transpose(1,2)  # Q: [batch_size, n_heads, len_q, d_k]
        K = self.W_K(input_K).view(batch_size, -1, self.n_heads, self.d_k).transpose(1,2)  # K: [batch_size, n_heads, len_k, d_k]
        V = self.W_V(input_V).view(batch_size, -1, self.n_heads, self.d_v).transpose(1,2)  # V: [batch_size, n_heads, len_v(=len_k), d_v]

        # context: [batch_size, n_heads, len_q, d_v], attn: [batch_size, n_heads, len_q, len_k]
        context = ScaledDotProductAttention()(Q, K, V, self.d_k)
        context = context.transpose(1, 2).reshape(batch_size, -1, self.n_heads * self.d_v) # context: [batch_size, len_q, n_heads * d_v]
        output = self.fc(context) # [batch_size, len_q, d_model]
        return self.norm(output + residual)

class PoswiseFeedForwardNet(nn.Module):
    def __init__(self,d_model=512,d_ff=2048):
        super(PoswiseFeedForwardNet, self).__init__()
        self.fc = nn.Sequential(
            nn.Linear(d_model, d_ff, bias=False),
            nn.ReLU(),
            nn.Linear(d_ff, d_model, bias=False)
        )
        self.norm = nn.LayerNorm(d_model)
    def forward(self, inputs):
        '''
        inputs: [batch_size, seq_len, d_model]
        '''
        residual = inputs 
        output = self.fc(inputs) 
        return self.norm(output + residual) 

class Multi_Heads(nn.Module):
    def __init__(self):
        super().__init__()
        self.multi_head1 = MultiHeadAttention()
        self.multi_head2 = MultiHeadAttention()
        self.Feed_forward = PoswiseFeedForwardNet() 
    def forward(self,x):
        x_shape = x.shape
        x = x.reshape(x_shape[0],x_shape[1],x_shape[2]*x_shape[3])
        x = x.permute(0,2,1)
        #-------------多头注意力强化特征-------------#
        x = self.multi_head1(x,x,x)
        x = self.multi_head2(x,x,x)
        #x = self.Feed_forward(x)
        x = x.permute(0,2,1)
        x = x.reshape(x_shape[0],x_shape[1],x_shape[2],x_shape[3])        
        return x

class CIF_Multihead(Backbone):
    '''
    This module implements :paper:`CIF_Multihead`.
    This module is used for object detection.
    '''
    _fuse_type: torch.jit.Final[str]

    def __init__(self,bottom_up):
        """
        Args:
            bottom_up (Backbone): module representing the bottom up subnetwork.
                Must be a subclass of :class:`Backbone`. The multi-scale feature
                maps generated by the bottom up network, and listed in `in_features`,
                are used to generate FPN levels.

        """
        super(CIF_Multihead,self).__init__()
        assert isinstance(bottom_up, Backbone)
        # Feature map strides and channels from the bottom up network (e.g. ResNet)
        input_shape = bottom_up.output_shape() #其实就是backbone
        #从Shapespec获取backbone的输出步长
        self.strides = input_shape["CIF"].stride
        #从Shapespec获取backbone的输出通道
        self.in_channels = input_shape["CIF"].channels

        self.features1 = bottom_up
        self.features2 = Multi_Heads()
        
    def forward(self,x):
        #----------------通道局部特征提取模块----------------#
        x = self.features1(x)
        
        #----------------多头注意力加分类器----------------#
        x = self.features2(x)
        return {"CIF_MultiHead":x}

    def output_shape(self):
        return {"CIF_MultiHead":ShapeSpec(channels=self.in_channels, stride=self.strides)}

@BACKBONE_REGISTRY.register()
def build_cif_multihead_backbone(cfg, input_shape):
    """
    Create a CIF_Multihead instance from config.

    Returns:
        CIF_Multihead: a :class:`CIF_Multihead` instance.
    """
    bottom_up = build_cif_backbone(cfg,input_shape)

    return CIF_Multihead(bottom_up)