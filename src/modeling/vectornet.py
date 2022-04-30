from typing import Dict, List, Tuple, NamedTuple, Any

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn, Tensor

from modeling.decoder import Decoder, DecoderResCat
from modeling.lib import MLP, GlobalGraph, LayerNorm, CrossAttention, GlobalGraphRes
import utils


class NewSubGraph(nn.Module):
 # 两层MLP + 3层res(hidden + global attention hidden), 最后返回eles的128维特征的subgraph特征和所有特征[-1, 128] (一个element可能包含多行)
    def __init__(self, hidden_size, depth=None):
        super(NewSubGraph, self).__init__()
        if depth is None:
            depth = args.sub_graph_depth
        self.layers = nn.ModuleList([MLP(hidden_size, hidden_size // 2) for _ in range(depth)])

        self.layer_0 = MLP(hidden_size)
        self.layers = nn.ModuleList([GlobalGraph(hidden_size, num_attention_heads=2) for _ in range(depth)])
        self.layers_2 = nn.ModuleList([LayerNorm(hidden_size) for _ in range(depth)])
        self.layers_3 = nn.ModuleList([LayerNorm(hidden_size) for _ in range(depth)])
        self.layers_4 = nn.ModuleList([GlobalGraph(hidden_size) for _ in range(depth)])
        if 'point_level-4-3' in args.other_params:
            self.layer_0_again = MLP(hidden_size)

    def forward(self, input_list: list):    # 两层MLP + 3层res(hidden + global attention hidden)
        num_ele = len(input_list)    # len(input_list) = 41, len(input_list[0]) = 9, input_list[0].shape = [9, 128]
        device = input_list[0].device
        hidden_states, lengths = utils.merge_tensors(input_list, device)    # hidden_states.shape = [ele, row_per_ele, hidden_size]
        hidden_size = hidden_states.shape[2]
        max_vector_num = hidden_states.shape[1]

        attention_mask = torch.zeros([num_ele, max_vector_num, max_vector_num], device=device)   # shape = [41, 9, 9]
        hidden_states = self.layer_0(hidden_states)

        if 'point_level-4-3' in args.other_params:
            hidden_states = self.layer_0_again(hidden_states)
        for i in range(num_ele):
            assert lengths[i] > 0
            attention_mask[i, :lengths[i], :lengths[i]].fill_(1)

        for layer_index, layer in enumerate(self.layers):
            temp = hidden_states
            # hidden_states = layer(hidden_states, attention_mask)
            # hidden_states = self.layers_2[layer_index](hidden_states)
            # hidden_states = F.relu(hidden_states) + temp
            hidden_states = layer(hidden_states, attention_mask)
            hidden_states = F.relu(hidden_states)
            hidden_states = hidden_states + temp
            hidden_states = self.layers_2[layer_index](hidden_states)

        return torch.max(hidden_states, dim=1)[0], torch.cat(utils.de_merge_tensors(hidden_states, lengths))    # [41, 128], [369, 128], 这里的[0]是获取max函数的values


class VectorNet(nn.Module):
    r"""
    VectorNet

    It has two main components, sub graph and global graph.

    Sub graph encodes a polyline as a single vector.
    """

    def __init__(self, args_: utils.Args):
        super(VectorNet, self).__init__()
        global args
        args = args_
        hidden_size = args.hidden_size

        self.point_level_sub_graph = NewSubGraph(hidden_size)
        self.point_level_cross_attention = CrossAttention(hidden_size)

        self.global_graph = GlobalGraph(hidden_size)    # self-atten
        if 'enhance_global_graph' in args.other_params:
            self.global_graph = GlobalGraphRes(hidden_size)     # 两个缩小到原来1/2维度的self-attention进行合并
        if 'laneGCN' in args.other_params:
            self.laneGCN_A2L = CrossAttention(hidden_size)
            self.laneGCN_L2L = GlobalGraphRes(hidden_size)
            self.laneGCN_L2A = CrossAttention(hidden_size)

        self.decoder = Decoder(args, self)

        if 'complete_traj' in args.other_params:
            self.decoder.complete_traj_cross_attention = CrossAttention(hidden_size)
            self.decoder.complete_traj_decoder = DecoderResCat(hidden_size, hidden_size * 3, out_features=self.decoder.future_frame_num * 2)

    def forward_encode_sub_graph(self, mapping: List[Dict], matrix: List[np.ndarray], polyline_spans: List[List[slice]],
                                 device, batch_size) -> Tuple[List[Tensor], List[Tensor]]:
        """ 两层MLP + 3层res(hidden + global attention hidden), 最后对lanes做laneGCN操作
        :param matrix: each value in list is vectors of all element (shape [-1, 128])
        :param polyline_spans: vectors of i_th element is matrix[polyline_spans[i]]
        :return: hidden states of all elements and hidden states of lanes
        """
        input_list_list = []
        # TODO(cyrushx): This is not used? Is it because input_list_list includes map data as well?
        # Yes, input_list_list includes map data, this will be used in the future release.
        map_input_list_list = []
        lane_states_batch = None
        for i in range(batch_size):
            input_list = []
            map_input_list = []
            map_start_polyline_idx = mapping[i]['map_start_polyline_idx']
            for j, polyline_span in enumerate(polyline_spans[i]):
                tensor = torch.tensor(matrix[i][polyline_span], device=device)
                input_list.append(tensor)
                if j >= map_start_polyline_idx:
                    map_input_list.append(tensor)

            input_list_list.append(input_list)  # len(input_list_list) = 2, len(input_list) = 37 (num_agent?), input_list[0].shape = [19, 128]
            map_input_list_list.append(map_input_list)  # len(map_input_list) = 33 (num_top_lane?), map_input_list[0].shape = [9, 128]

        if True:
            element_states_batch = []
            for i in range(batch_size):
                a, b = self.point_level_sub_graph(input_list_list[i])
                element_states_batch.append(a)  # [77, 128], [37, 128]

        if 'stage_one' in args.other_params:
            lane_states_batch = []
            for i in range(batch_size):
                a, b = self.point_level_sub_graph(map_input_list_list[i])   # a.shape = [33, 128]
                lane_states_batch.append(a) # [47, 128], [33, 128]

        if 'laneGCN' in args.other_params:
            inputs_before_laneGCN, inputs_lengths_before_laneGCN = utils.merge_tensors(element_states_batch, device=device)
            for i in range(batch_size):
                map_start_polyline_idx = mapping[i]['map_start_polyline_idx']
                agents = element_states_batch[i][:map_start_polyline_idx]
                lanes = element_states_batch[i][map_start_polyline_idx:]
                if 'laneGCN-4' in args.other_params:    # cross-attention, query是lane, key和value是lanns和AGENT的堆叠
                    lanes = lanes + self.laneGCN_A2L(lanes.unsqueeze(0), torch.cat([lanes, agents[0:1]]).unsqueeze(0)).squeeze(0)   # cross-atten, 仅仅lanes被修改
                else:
                    lanes = lanes + self.laneGCN_A2L(lanes.unsqueeze(0), agents.unsqueeze(0)).squeeze(0)
                    lanes = lanes + self.laneGCN_L2L(lanes.unsqueeze(0)).squeeze(0)
                    agents = agents + self.laneGCN_L2A(agents.unsqueeze(0), lanes.unsqueeze(0)).squeeze(0)
                element_states_batch[i] = torch.cat([agents, lanes])

        return element_states_batch, lane_states_batch  # batch_size length list, 第一个是[ele, 128], 其中的lane经过lanegcn, 第二个是[lane, 128], 其中的lane没有经过lanegcn

    # @profile
    def forward(self, mapping: List[Dict], device):
        import time
        global starttime
        starttime = time.time()

        matrix = utils.get_from_mapping(mapping, 'matrix')  # list of matrix
        # TODO(cyrushx): Can you explain the structure of polyline spans?
        # vectors of i_th element is matrix[polyline_spans[i]]
        polyline_spans = utils.get_from_mapping(mapping, 'polyline_spans')

        batch_size = len(matrix)
        # for i in range(batch_size):
        # polyline_spans[i] = [slice(polyline_span[0], polyline_span[1]) for polyline_span in polyline_spans[i]]

        if args.argoverse:
            utils.batch_init(mapping)   # 之前将坐标系旋转, 这里计算将坐标系转回去所需要的original angle和原点

        element_states_batch, lane_states_batch = self.forward_encode_sub_graph(mapping, matrix, polyline_spans, device, batch_size)    # ele_batch[0] = [ele, 128]

        inputs, inputs_lengths = utils.merge_tensors(element_states_batch, device=device)   # inputs.shape = [batch, max_ele, 128]
        max_poly_num = max(inputs_lengths)
        attention_mask = torch.zeros([batch_size, max_poly_num, max_poly_num], device=device)
        for i, length in enumerate(inputs_lengths):
            attention_mask[i][:length][:length].fill_(1)

        hidden_states = self.global_graph(inputs, attention_mask, mapping)  # hidden_states.shape == inputs.shape

        utils.logging('time3', round(time.time() - starttime, 2), 'secs')

        return self.decoder(mapping, batch_size, lane_states_batch, inputs, inputs_lengths, hidden_states, device)
