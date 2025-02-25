import torch
import torch.nn as nn
from transformers import BertModel
import math
from torch.nn import functional as F


class MaskConv(nn.Module):
    def __init__(self, seq_module):
        super(MaskConv, self).__init__()
        self.seq_module = seq_module

    def forward(self, x, lengths):
        """
        :param x: The input of size BxCxDxT
        :param lengths: The actual length of each sequence in the batch
        :return: Masked output from the module
        """
        for module in self.seq_module:
            x = module(x)
            mask = torch.BoolTensor(x.size()).fill_(0)
            if x.is_cuda:
                mask = mask.cuda()
            for i, length in enumerate(lengths):
                length = length.item()
                if (mask[i].size(2) - length) > 0:
                    mask[i].narrow(2, length, mask[i].size(2) - length).fill_(1)
            x = x.masked_fill(mask, 0)
        return x, lengths


class AudioStream(nn.Module):
    def __init__(self, input_size, hidden_size=128, n_layers=2,
                 input_dropout_p=0, dropout_p=0,
                 bidirectional=True, rnn_cell='gru', variable_lengths=False):
        super(AudioStream, self).__init__()
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.n_layers = n_layers
        self.dropout_p = 0.5
        self.variable_lengths = variable_lengths

        if rnn_cell.lower() == 'lstm':
            self.rnn_cell = nn.LSTM
        elif rnn_cell.lower() == 'gru':
            self.rnn_cell = nn.GRU
        else:
            raise ValueError("Unsupported RNN Cell: {0}".format(rnn_cell))
            
        outputs_channel = 64
        self.conv = MaskConv(nn.Sequential(
            nn.Conv2d(1, outputs_channel, kernel_size=(41, 11), stride=(2, 2), padding=(20, 5)),
            nn.BatchNorm2d(outputs_channel),
            nn.Hardtanh(0, 20, inplace=True),
            nn.Conv2d(outputs_channel, outputs_channel, kernel_size=(21, 11), stride=(2, 1), padding=(10, 5)),
            nn.BatchNorm2d(outputs_channel),
            nn.Hardtanh(0, 20, inplace=True),
        ))
        
        rnn_input_dims = int(math.floor(input_size + 2 * 20 - 41) / 2 + 1)
        rnn_input_dims = int(math.floor(rnn_input_dims + 2 * 10 - 21) / 2 + 1)
        rnn_input_dims *= outputs_channel
        
        self.rnn =  self.rnn_cell(rnn_input_dims, self.hidden_size, self.n_layers, dropout=self.dropout_p, bidirectional=self.bidirectional)
        

    def forward(self, input_var, input_lengths=None):
        """
        param:input_var: Encoder inputs, Spectrogram, Shape=(B,1,D,T)
        param:input_lengths: inputs sequence length without zero-pad
        """

        output_lengths = self.get_seq_lens(input_lengths)

        x = input_var # (B,1,D,T)
        x, _ = self.conv(x, output_lengths) # (B, C, D, T)
        
        x_size = x.size()
        x = x.view(x_size[0], x_size[1] * x_size[2], x_size[3]) # (B, C * D, T)
        x = x.transpose(1, 2).transpose(0, 1).contiguous() # (T, B, D)

        x = nn.utils.rnn.pack_padded_sequence(x, output_lengths,enforce_sorted=False)
        x, h_state = self.rnn(x)
        x, _ = nn.utils.rnn.pad_packed_sequence(x)
        
        x = x.transpose(0, 1) # (B, T, D)
        

        return x, h_state
    

    def get_seq_lens(self, input_length):
        seq_len = input_length
        for m in self.conv.modules():
            if type(m) == nn.modules.conv.Conv2d :
                seq_len = ((seq_len + 2 * m.padding[1] - m.dilation[1] * (m.kernel_size[1] - 1) - 1) / m.stride[1] + 1)
            
            
        return seq_len.int()
        
    
        

class AudioOnly(nn.Module):
    def __init__(self, input_size,num_classes):
        super().__init__()
        self.num_classes = num_classes
        self.audio_stream = AudioStream(input_size)
      
        self.fc = nn.Linear(2*256,num_classes)
    
    def forward(self, audio_features, audio_lens):
        audio_feats,_ = self.audio_stream(audio_features, audio_lens)
        mu = torch.mean(audio_feats,dim=1)
        std = torch.var(audio_feats, dim=1)
        stat_pool = torch.cat((mu, std), dim=1)
        out = self.fc(stat_pool)
        return out
        
