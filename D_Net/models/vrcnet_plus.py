import numpy as np
import os
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.utils.data
import torch.nn.functional as F
from utils.model_utils import *
from utils.train_utils import *
from utils.model_utils import calc_emd, calc_cd, calc_dcd
from models.pcn import PCN_encoder

proj_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(proj_dir, "utils/Pointnet2.PyTorch/pointnet2"))
import pointnet2_utils as pn2
sys.path.append("../MVP_new/completion")
from plot import draw_any_set


class SA_module(nn.Module):
    def __init__(self, in_planes, rel_planes, mid_planes, out_planes, share_planes=8, k=16):
        super(SA_module, self).__init__()
        self.share_planes = share_planes
        self.k = k
        self.conv1 = nn.Conv2d(in_planes, rel_planes, kernel_size=1)
        self.conv2 = nn.Conv2d(in_planes, rel_planes, kernel_size=1)
        self.conv3 = nn.Conv2d(in_planes, mid_planes, kernel_size=1)

        self.conv_w = nn.Sequential(nn.ReLU(inplace=False),
                                    nn.Conv2d(rel_planes * (k + 1), mid_planes // share_planes, kernel_size=1,
                                              bias=False),
                                    nn.ReLU(inplace=False),
                                    nn.Conv2d(mid_planes // share_planes, k * mid_planes // share_planes,
                                              kernel_size=1))
        self.activation_fn = nn.ReLU(inplace=False)

        self.conv_out = nn.Conv2d(mid_planes, out_planes, kernel_size=1)

    def forward(self, input):
        x, idx = input
        batch_size, _, _, num_points = x.size()
        identity = x  # B C 1 N
        x = self.activation_fn(x)
        xn = get_edge_features(x, idx)  # B C K N
        x1, x2, x3 = self.conv1(x), self.conv2(xn), self.conv3(xn)

        x2 = x2.view(batch_size, -1, 1, num_points).contiguous()  # B kC 1 N
        w = self.conv_w(torch.cat([x1, x2], 1)).view(batch_size, -1, self.k, num_points)
        w = w.repeat(1, self.share_planes, 1, 1)
        out = w * x3
        out = torch.sum(out, dim=2, keepdim=True)

        out = self.activation_fn(out)
        out = self.conv_out(out)  # B C 1 N
        out += identity
        return [out, idx]


class Folding(nn.Module):
    def __init__(self, input_size, output_size, step_ratio, global_feature_size=1024, num_models=1):
        super(Folding, self).__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.step_ratio = step_ratio
        self.num_models = num_models

        self.conv = nn.Conv1d(input_size + global_feature_size + 2, output_size, 1, bias=True)

        sqrted = int(math.sqrt(step_ratio)) + 1
        for i in range(1, sqrted + 1).__reversed__():
            if (step_ratio % i) == 0:
                num_x = i
                num_y = step_ratio // i
                break

        grid_x = torch.linspace(-0.2, 0.2, steps=num_x)
        grid_y = torch.linspace(-0.2, 0.2, steps=num_y)

        x, y = torch.meshgrid(grid_x, grid_y)  # x, y shape: (2, 1)
        self.grid = torch.stack([x, y], dim=-1).view(-1, 2)  # (2, 2)

    def forward(self, point_feat, global_feat):
        batch_size, num_features, num_points = point_feat.size()
        point_feat = point_feat.transpose(1, 2).contiguous().unsqueeze(2).repeat(1, 1, self.step_ratio, 1).view(
            batch_size,
            -1, num_features).transpose(1, 2).contiguous()
        global_feat = global_feat.unsqueeze(2).repeat(1, 1, num_points * self.step_ratio).repeat(self.num_models, 1, 1)
        grid_feat = self.grid.unsqueeze(0).repeat(batch_size, num_points, 1).transpose(1, 2).contiguous().cuda()
        features = torch.cat([global_feat, point_feat, grid_feat], axis=1)
        features = F.relu(self.conv(features))
        return features


class Linear_ResBlock(nn.Module):
    def __init__(self, input_size=1024, output_size=256):
        super(Linear_ResBlock, self).__init__()
        self.conv1 = nn.Linear(input_size, input_size)
        self.conv2 = nn.Linear(input_size, output_size)
        self.conv_res = nn.Linear(input_size, output_size)

        self.af = nn.ReLU(inplace=True)

    def forward(self, feature):
        return self.conv2(self.af(self.conv1(self.af(feature)))) + self.conv_res(feature)


class SK_SA_module(nn.Module):
    def __init__(self, in_planes, rel_planes, mid_planes, out_planes, share_planes=8, k=[10, 20], r=2, L=32):
        super(SK_SA_module, self).__init__()

        self.num_kernels = len(k)
        d = max(int(out_planes / r), L)

        self.sams = nn.ModuleList([])

        for i in range(len(k)):
            self.sams.append(SA_module(in_planes, rel_planes, mid_planes, out_planes, share_planes, k[i]))

        self.fc = nn.Linear(out_planes, d)
        self.fcs = nn.ModuleList([])

        for i in range(len(k)):
            self.fcs.append(nn.Linear(d, out_planes))

        self.softmax = nn.Softmax(dim=1)
        self.af = nn.ReLU(inplace=False)

    def forward(self, input):
        x, idxs = input
        assert (self.num_kernels == len(idxs))
        for i, sam in enumerate(self.sams):
            fea, _ = sam([x, idxs[i]])
            fea = self.af(fea)
            fea = fea.unsqueeze(dim=1)
            if i == 0:
                feas = fea
            else:
                feas = torch.cat([feas, fea], dim=1)

        fea_U = torch.sum(feas, dim=1)
        fea_s = fea_U.mean(-1).mean(-1)
        fea_z = self.fc(fea_s)

        for i, fc in enumerate(self.fcs):
            vector = fc(fea_z).unsqueeze_(dim=1)
            if i == 0:
                attention_vectors = vector
            else:
                attention_vectors = torch.cat([attention_vectors, vector], dim=1)

        attention_vectors = self.softmax(attention_vectors)
        attention_vectors = attention_vectors.unsqueeze(-1).unsqueeze(-1)
        fea_v = (feas * attention_vectors).sum(dim=1)
        return [fea_v, idxs]


class SKN_Res_unit(nn.Module):
    def __init__(self, input_size, output_size, k=[10, 20], layers=1):
        super(SKN_Res_unit, self).__init__()
        self.conv1 = nn.Conv2d(input_size, output_size, 1, bias=False)
        self.sam = self._make_layer(output_size, output_size // 16, output_size // 4, output_size, int(layers), 8, k=k)
        self.conv2 = nn.Conv2d(output_size, output_size, 1, bias=False)
        self.conv_res = nn.Conv2d(input_size, output_size, 1, bias=False)
        self.af = nn.ReLU(inplace=False)

    def _make_layer(self, in_planes, rel_planes, mid_planes, out_planes, blocks, share_planes=8, k=16):
        layers = []
        for _ in range(0, blocks):
            layers.append(SK_SA_module(in_planes, rel_planes, mid_planes, out_planes, share_planes, k))
        return nn.Sequential(*layers)

    def forward(self, feat, idx):
        x, _ = self.sam([self.conv1(feat), idx])
        x = self.conv2(self.af(x))
        return x + self.conv_res(feat)


class SA_SKN_Res_encoder(nn.Module):
    def __init__(self, input_size=3, k=[10, 20], pk=16, output_size=64, layers=[2, 2, 2, 2],
                 pts_num=[3072, 1536, 768, 384]):
        super(SA_SKN_Res_encoder, self).__init__()
        self.init_channel = 64

        c1 = self.init_channel
        self.sam_res1 = SKN_Res_unit(input_size, c1, k, int(layers[0]))

        c2 = c1 * 2
        self.sam_res2 = SKN_Res_unit(c2, c2, k, int(layers[1]))

        c3 = c2 * 2
        self.sam_res3 = SKN_Res_unit(c3, c3, k, int(layers[2]))

        c4 = c3 * 2
        self.sam_res4 = SKN_Res_unit(c4, c4, k, int(layers[3]))

        self.conv5 = nn.Conv2d(c4, 1024, 1)

        self.fc1 = nn.Linear(1024, 512)
        self.fc2 = nn.Linear(512, 1024)

        self.conv6 = nn.Conv2d(c4 + 1024, c4, 1)
        self.conv7 = nn.Conv2d(c3 + c4, c3, 1)
        self.conv8 = nn.Conv2d(c2 + c3, c2, 1)
        self.conv9 = nn.Conv2d(c1 + c2, c1, 1)

        self.conv_out = nn.Conv2d(c1, output_size, 1)
        self.dropout = nn.Dropout()
        self.af = nn.ReLU(inplace=False)
        self.k = k
        self.pk = pk
        self.rate = 2

        self.pts_num = pts_num

    def _make_layer(self, in_planes, rel_planes, mid_planes, out_planes, blocks, share_planes=8, k=16):
        layers = []
        for _ in range(0, blocks):
            layers.append(SK_SA_module(in_planes, rel_planes, mid_planes, out_planes, share_planes, k))
        return nn.Sequential(*layers)

    def _edge_pooling(self, features, points, rate=2, k=16, sample_num=None):
        features = features.squeeze(2)

        if sample_num is None:
            input_points_num = int(features.size()[2])
            sample_num = input_points_num // rate

        ds_features, p_idx, pn_idx, ds_points = edge_preserve_sampling(features, points, sample_num, k)
        ds_features = ds_features.unsqueeze(2)
        return ds_features, p_idx, pn_idx, ds_points

    def _edge_unpooling(self, features, src_pts, tgt_pts):
        features = features.squeeze(2)
        idx, weight = three_nn_upsampling(tgt_pts, src_pts)
        features = pn2.three_interpolate(features, idx, weight)
        features = features.unsqueeze(2)
        return features

    def forward(self, features):
        batch_size, _, num_points = features.size()
        pt1 = features[:, 0:3, :]

        idx1 = []
        for i in range(len(self.k)):
            idx = knn(pt1, self.k[i])
            idx1.append(idx)

        pt1 = pt1.transpose(1, 2).contiguous()

        x = features.unsqueeze(2)
        x = self.sam_res1(x, idx1)
        x1 = self.af(x)

        x, _, _, pt2 = self._edge_pooling(x1, pt1, self.rate, self.pk, self.pts_num[1])
        idx2 = []
        for i in range(len(self.k)):
            idx = knn(pt2.transpose(1, 2).contiguous(), self.k[i])
            idx2.append(idx)

        x = self.sam_res2(x, idx2)
        x2 = self.af(x)

        x, _, _, pt3 = self._edge_pooling(x2, pt2, self.rate, self.pk, self.pts_num[2])
        idx3 = []
        for i in range(len(self.k)):
            idx = knn(pt3.transpose(1, 2).contiguous(), self.k[i])
            idx3.append(idx)

        x = self.sam_res3(x, idx3)
        x3 = self.af(x)

        x, _, _, pt4 = self._edge_pooling(x3, pt3, self.rate, self.pk, self.pts_num[3])
        idx4 = []
        for i in range(len(self.k)):
            idx = knn(pt4.transpose(1, 2).contiguous(), self.k[i])
            idx4.append(idx)

        x = self.sam_res4(x, idx4)
        x4 = self.af(x)
        x = self.conv5(x4)
        x, _ = torch.max(x, -1)
        x = x.view(batch_size, -1)
        x = self.dropout(self.af(self.fc2(self.dropout(self.af(self.fc1(x))))))

        x = x.unsqueeze(2).repeat(1, 1, self.pts_num[3]).unsqueeze(2)
        x = self.af(self.conv6(torch.cat([x, x4], 1)))
        x = self._edge_unpooling(x, pt4, pt3)
        x = self.af(self.conv7(torch.cat([x, x3], 1)))
        x = self._edge_unpooling(x, pt3, pt2)
        x = self.af(self.conv8(torch.cat([x, x2], 1)))
        x = self._edge_unpooling(x, pt2, pt1)
        x = self.af(self.conv9(torch.cat([x, x1], 1)))
        x = self.conv_out(x)
        x = x.squeeze(2)
        return x


class MSAP_SKN_decoder(nn.Module):
    def __init__(self, num_coarse_raw, num_fps, num_coarse, num_fine, layers=[2, 2, 2, 2], knn_list=[10, 20], pk=10,
                 points_label=False, local_folding=False, ipts=[], over_scale=1):
        super(MSAP_SKN_decoder, self).__init__()
        self.num_coarse_raw = num_coarse_raw
        self.num_fps = num_fps
        self.num_coarse = num_coarse
        self.num_fine = num_fine
        self.points_label = points_label
        self.local_folding = local_folding

        self.fc1 = nn.Linear(1024, 1024)
        self.fc2 = nn.Linear(1024, 1024)
        self.fc3 = nn.Linear(1024, num_coarse_raw * 3)

        self.dense_feature_size = 256
        self.expand_feature_size = 64

        if points_label:
            self.input_size = 4
        else:
            self.input_size = 3

        self.encoder = SA_SKN_Res_encoder(input_size=self.input_size, k=knn_list, pk=pk,
                                          output_size=self.dense_feature_size, layers=layers)

        self.up_scale = int(np.ceil(num_fine / (num_coarse_raw + 2048)))
        self.up_scale *= over_scale
        if self.up_scale >= 2:
            self.expansion1 = EF_expansion(input_size=self.dense_feature_size, output_size=self.expand_feature_size,
                                           step_ratio=self.up_scale, k=4)
            self.conv_cup1 = nn.Conv1d(self.expand_feature_size, self.expand_feature_size, 1)
        else:
            self.expansion1 = None
            self.conv_cup1 = nn.Conv1d(self.dense_feature_size, self.expand_feature_size, 1)

        self.conv_cup2 = nn.Conv1d(self.expand_feature_size, 3, 1, bias=True)

        self.conv_s1 = nn.Conv1d(self.expand_feature_size, 16, 1, bias=True)
        self.conv_s2 = nn.Conv1d(16, 8, 1, bias=True)
        self.conv_s3 = nn.Conv1d(8, 1, 1, bias=True)

        if self.local_folding:
            self.expansion2 = Folding(input_size=self.expand_feature_size, output_size=self.dense_feature_size,
                                      step_ratio=(num_fine // num_coarse))
        else:
            self.expansion2 = EF_expansion(input_size=self.expand_feature_size, output_size=self.dense_feature_size,
                                           step_ratio=(num_fine // num_coarse), k=4)

        self.conv_f1 = nn.Conv1d(self.dense_feature_size, self.expand_feature_size, 1)
        self.conv_f2 = nn.Conv1d(self.expand_feature_size, 3, 1)

        self.af = nn.ReLU(inplace=False)

        self.ipts = ipts
        if 'point_d' in self.ipts:
            self.point_d = Point_Discriminator(global_feat_dim=1024, local_feat_dim=self.dense_feature_size, point_feat_dim=3)

    def forward(self, global_feat, point_input, detach=True):
        batch_size = global_feat.size()[0]

        coarse_raw = self.fc3(self.af(self.fc2(self.af(self.fc1(global_feat))))).view(batch_size, 3,
                                                                                      self.num_coarse_raw)

        input_points_num = point_input.size()[2]
        org_points_input = point_input

        if self.points_label:
            id0 = torch.zeros(coarse_raw.shape[0], 1, coarse_raw.shape[2]).cuda().contiguous()
            coarse_input = torch.cat( (coarse_raw, id0), 1)
            id1 = torch.ones(org_points_input.shape[0], 1, org_points_input.shape[2]).cuda().contiguous()
            org_points_input = torch.cat( (org_points_input, id1), 1)
        else:
            coarse_input = coarse_raw

        points = torch.cat((coarse_input, org_points_input), 2)
        dense_feat = self.encoder(points)

        if self.up_scale >= 2:
            expand_feat = self.expansion1(dense_feat)
        else:
            expand_feat = dense_feat

        coarse_features = self.af(self.conv_cup1(expand_feat))
        coarse_high = self.conv_cup2(coarse_features)

        # query frequency predictor
        ipt_query, point_d = None, None
        if len(self.ipts) > 0:
            if detach:
                global_feat_, coarse_, dense_feat_ = global_feat.detach(), coarse_raw.detach(), dense_feat.detach()
            else:
                global_feat_, coarse_, dense_feat_ = global_feat, coarse_raw, dense_feat
            if 'ipt_query' in self.ipts:
                ipt_query = self.ipt_query(global_feat_, coarse_).squeeze()
                ipt_query = torch.relu(ipt_query) # if activate else ipt_query

            if 'point_d' in self.ipts:
                point_d = self.point_d(global_feat_, coarse_[:,:3,:], dense_feat_[:,:,:coarse_.shape[-1]]).squeeze()




        if coarse_high.size()[2] > self.num_fps:
            idx_fps = pn2.furthest_point_sample(coarse_high.transpose(1, 2).contiguous(), self.num_fps)
            coarse_fps = pn2.gather_operation(coarse_high, idx_fps)
            coarse_features = pn2.gather_operation(coarse_features, idx_fps)
        else:
            coarse_fps = coarse_high

        if coarse_fps.size()[2] > self.num_coarse:
            scores = F.softplus(self.conv_s3(self.af(self.conv_s2(self.af(self.conv_s1(coarse_features))))))
            idx_scores = scores.topk(k=self.num_coarse, dim=2)[1].view(batch_size, -1).int()
            coarse = pn2.gather_operation(coarse_fps, idx_scores)
            coarse_features = pn2.gather_operation(coarse_features, idx_scores)
        else:
            coarse = coarse_fps

        if coarse.size()[2] < self.num_fine:
            if self.local_folding:
                up_features = self.expansion2(coarse_features, global_feat)
                center = coarse.transpose(2, 1).contiguous().unsqueeze(2).repeat(1, 1, self.num_fine // self.num_coarse,
                                                                                 1).view(batch_size, self.num_fine,
                                                                                         3).transpose(2, 1).contiguous()
                fine = self.conv_f2(self.af(self.conv_f1(up_features))) + center
            else:
                up_features = self.expansion2(coarse_features)
                fine = self.conv_f2(self.af(self.conv_f1(up_features)))
        else:
            assert (coarse.size()[2] == self.num_fine)
            fine = coarse

        return coarse_raw, coarse_high, coarse, fine, ipt_query, point_d


class Model(nn.Module):
    def __init__(self, args, size_z=128, global_feature_size=1024, ipts=['point_d']):
        super(Model, self).__init__()

        layers = [int(i) for i in args.layers.split(',')]
        knn_list = [int(i) for i in args.knn_list.split(',')]

        self.size_z = size_z
        self.distribution_loss = args.distribution_loss
        self.train_loss = args.loss
        self.loss_opts = args.loss_opts
        self.eval_emd = args.eval_emd
        self.encoder = PCN_encoder(output_size=global_feature_size)
        self.posterior_infer1 = Linear_ResBlock(input_size=global_feature_size, output_size=global_feature_size)
        self.posterior_infer2 = Linear_ResBlock(input_size=global_feature_size, output_size=size_z * 2)
        self.prior_infer = Linear_ResBlock(input_size=global_feature_size, output_size=size_z * 2)
        self.generator = Linear_ResBlock(input_size=size_z, output_size=global_feature_size)
        self.decoder = MSAP_SKN_decoder(num_fps=args.num_fps, num_fine=args.num_points, num_coarse=args.num_coarse,
                                        num_coarse_raw=args.num_coarse_raw, layers=layers, knn_list=knn_list,
                                        pk=args.pk, local_folding=args.local_folding, points_label=args.points_label,
                                        ipts=ipts, over_scale=args.over_scale)
        if self.loss_opts is not None:
            for k,v in self.loss_opts.items():
                print("{} : {}".format(k,v))

        self.detach = True
        self.ipts = ipts
        self.prob_a, self.prob_b = getattr(args, "prob_ab", [9, -1])
        print("a: {} | b: {}".format(self.prob_a, self.prob_b))
        self.prob_sample = getattr(args, "prob_sample", False)

    def compute_kernel(self, x, y):
        x_size = x.size()[0]
        y_size = y.size()[0]
        dim = x.size()[1]

        tiled_x = x.unsqueeze(1).repeat(1, y_size, 1)
        tiled_y = y.unsqueeze(0).repeat(x_size, 1, 1)
        return torch.exp(-torch.mean((tiled_x - tiled_y)**2, dim=2) / float(dim))

    def mmd_loss(self, x, y):
        x_kernel = self.compute_kernel(x, x)
        y_kernel = self.compute_kernel(y, y)
        xy_kernel = self.compute_kernel(x, y)
        return torch.mean(x_kernel) + torch.mean(y_kernel) - 2 * torch.mean(xy_kernel)

    def forward(self, x, gt, is_training=True, mean_feature=None, alpha=None, test_emd=True):
        num_input = x.size()[2]

        if is_training:
            y = pn2.gather_operation(gt.transpose(1, 2).contiguous(), pn2.furthest_point_sample(gt, num_input))
            gt = torch.cat([gt, gt], dim=0)
            points = torch.cat([x, y], dim=0)
            x = torch.cat([x, x], dim=0)
        else:
            points = x
        feat = self.encoder(points)

        if is_training:
            feat_x, feat_y = feat.chunk(2)
            o_x = self.posterior_infer2(self.posterior_infer1(feat_x))
            q_mu, q_std = torch.split(o_x, self.size_z, dim=1)
            o_y = self.prior_infer(feat_y)
            p_mu, p_std = torch.split(o_y, self.size_z, dim=1)
            q_std = F.softplus(q_std)
            p_std = F.softplus(p_std)
            q_distribution = torch.distributions.Normal(q_mu, q_std)
            p_distribution = torch.distributions.Normal(p_mu, p_std)
            p_distribution_fix = torch.distributions.Normal(p_mu.detach(), p_std.detach())
            m_distribution = torch.distributions.Normal(torch.zeros_like(p_mu), torch.ones_like(p_std))
            z_q = q_distribution.rsample()
            z_p = p_distribution.rsample()
            z = torch.cat([z_q, z_p], dim=0)
            feat = torch.cat([feat_x, feat_x], dim=0)

        else:
            o_x = self.posterior_infer2(self.posterior_infer1(feat))
            q_mu, q_std = torch.split(o_x, self.size_z, dim=1)
            q_std = F.softplus(q_std)
            q_distribution = torch.distributions.Normal(q_mu, q_std)
            p_distribution = q_distribution
            p_distribution_fix = p_distribution
            m_distribution = p_distribution
            z = q_distribution.rsample()

        feat += self.generator(z)

        coarse_raw, coarse_high, coarse, fine, ipt_query, point_d = self.decoder(
            feat, x, detach=self.detach)
        coarse_raw = coarse_raw.transpose(1, 2).contiguous()
        coarse_high = coarse_high.transpose(1, 2).contiguous()
        coarse = coarse.transpose(1, 2).contiguous()
        fine = fine.transpose(1, 2).contiguous()

        if is_training:
            if self.distribution_loss == 'MMD':
                z_m = m_distribution.rsample()
                z_q = q_distribution.rsample()
                z_p = p_distribution.rsample()
                z_p_fix = p_distribution_fix.rsample()
                dl_rec = self.mmd_loss(z_m, z_p)
                dl_g = self.mmd_loss2(z_q, z_p_fix)
            elif self.distribution_loss == 'KLD':
                dl_rec = torch.distributions.kl_divergence(m_distribution, p_distribution)
                dl_g = torch.distributions.kl_divergence(p_distribution_fix, q_distribution)
            else:
                raise NotImplementedError('Distribution loss is either MMD or KLD')


            # import ipdb; ipdb.set_trace()
            total_train_loss = None
            losses = dict()
            if self.train_loss == 'cd':
                loss1, _,  c_dist1, c_dist2, c_idx1, c_idx2 = calc_cd(coarse_raw, gt, return_raw=True)
                loss2, _ = calc_cd(coarse_high, gt)
                loss3, _ = calc_cd(coarse, gt)
                loss4, _, dist1, dist2, idx1, idx2 = calc_cd(fine, gt, return_raw=True)
            elif self.train_loss == 'emd':
                loss1, _,  c_dist1, c_dist2, c_idx1, c_idx2 = calc_cd(coarse_raw, gt, return_raw=True)
                loss2, _ = calc_cd(coarse_high, gt)
                loss3, _ = calc_cd(coarse, gt)
                loss4 = calc_emd(fine, gt)
            elif self.train_loss == 'dcd':
                t_alpha = self.loss_opts["alpha"]
                n_lambda = self.loss_opts["lambda"]
                loss1, _, _,  c_dist1, c_dist2, c_idx1, c_idx2 = calc_dcd(coarse_raw, gt, alpha=t_alpha * 1, n_lambda=n_lambda, return_raw=True)
                loss2, _, _ = calc_dcd(coarse_high, gt, alpha=t_alpha, n_lambda=n_lambda)
                loss3, _, _ = calc_dcd(coarse, gt, alpha=t_alpha, n_lambda=n_lambda)
                dcd, cd_p, cd_t, dist1, dist2, idx1, idx2 = calc_dcd(fine, gt, alpha=t_alpha, return_raw=True, n_lambda=n_lambda)
                loss4 = cd_p * 50
            else:
                raise NotImplementedError

            if total_train_loss is None:
                total_train_loss = loss1.mean() * 10 + loss2.mean() * 0.5 + loss3.mean() + loss4.mean() * alpha
            total_train_loss += (dl_rec.mean() + dl_g.mean()) * 20

            # loss for the discriminator
            if 'point_d' in self.ipts:
                q_count = self.update_query(self.decoder.num_coarse_raw, c_idx1, per_sample=True).detach()
                scale = gt.shape[1] / q_count.shape[1]
                ipt_target = (q_count==0).float() * torch.sqrt(c_dist2 + 1e-8) * 10 - (q_count > 0).float() * torch.log2(q_count/scale + 1)
                loss_ipt = F.mse_loss(point_d, ipt_target.detach()) * 0.1
                losses.update(loss_ipt=loss_ipt)
            for n, loss in losses.items():
                total_train_loss += loss.mean()

            return fine, loss4, total_train_loss, losses
        else:
            if self.prob_sample:
                batch_size, num_points = fine.shape[0], self.decoder.num_coarse_raw * self.decoder.up_scale
                prob = torch.sigmoid(point_d * (-1.) * self.prob_a + self.prob_b).unsqueeze(-1).repeat(1, 1, self.decoder.up_scale).reshape(batch_size, num_points)
                rand = torch.rand(prob.shape).cuda()
                fine_probs = []
                for b in range(rand.shape[0]):
                    sample = coarse_high[:,:num_points][b, rand[b] < prob[b]]
                    all_samples = torch.cat([sample, coarse_high[b,num_points:]], dim=0).unsqueeze(0)
                    idx_fps = pn2.furthest_point_sample(all_samples, self.decoder.num_fine)
                    sample_fps = pn2.gather_operation(all_samples.transpose(1, 2), idx_fps).transpose(1, 2)
                    fine_probs.append(sample_fps)
                fine = torch.cat(fine_probs)

            # guided down-sampling
            if self.eval_emd and test_emd:
                emd = calc_emd(fine, gt, eps=0.004, iterations=3000)
            else:
                emd = torch.ones(1).cuda()
            cd_p, cd_t, f1 = calc_cd(fine, gt, calc_f1=True)
            dcd, _, _ = calc_dcd(fine, gt)
            res = {'out1': coarse_raw, 'out2': fine, 'emd': emd, 'dcd': dcd, 'cd_t': cd_t, 'f1': f1}

            return res

    def update_query(self, num_points, gt2x_idx, per_sample=False):
        if per_sample:
            counts = torch.zeros([gt2x_idx.shape[0], num_points]).cuda()
            for i, this_idxs in enumerate(gt2x_idx):
                v, this_count = torch.unique(this_idxs, return_counts=True)
                counts[i,v.long()] = this_count.float()
        else:
            q_idx, counts = torch.unique(gt2x_idx, return_counts=True)
        return counts


class Point_Discriminator(nn.Module):
    def __init__(self, global_feat_dim=1024, local_feat_dim=0, point_feat_dim=3, ef_out_dim=128,
                 pk=4, with_xyz=True):
        super(Point_Discriminator, self).__init__()

        cat_feature_num = local_feat_dim + point_feat_dim if with_xyz else local_feat_dim
        self.ef_mlp = nn.Sequential(
            nn.Conv2d(cat_feature_num, 256, 1),
            nn.LeakyReLU(inplace=False),
            nn.Conv2d(256, ef_out_dim, 1)
        )

        cat_feature_num = global_feat_dim + local_feat_dim + ef_out_dim + point_feat_dim
        self.mlp = nn.Sequential(
            nn.Conv1d(cat_feature_num, 1024, 1),
            nn.LeakyReLU(inplace=False),
            nn.Conv1d(1024, 256, 1),
            nn.LeakyReLU(inplace=False),
            nn.Conv1d(256, 1, 1)
        )
        self.pk = pk
        self.with_xyz = with_xyz

    def forward(self, global_feat, points, local_feats=None, knn_idxs=None,
                minus_xyz_center=True, minus_feat_center=False):
        # edge
        # B, C, N -> B, N
        B, _, N = points.size()
        assert self.pk > 1
        idx = knn(points, k = self.pk + 1 ) # [:,:,1:] # batch_size, num_points, k
        x_knn = get_edge_features(points, idx)  # B C K N
        f_knn = get_edge_features(local_feats, idx)  # B C K N

        if minus_xyz_center:
            x_knn[:,:,1:,:] = points.unsqueeze(2) - x_knn[:,:,1:,:]
        if minus_feat_center:
            f_knn[:,:,1:,:] = local_feats.unsqueeze(2) - f_knn[:,:,1:,:]
        if self.with_xyz:
            f_knn = torch.cat([f_knn, x_knn], dim=1)

        f_knn = self.ef_mlp(f_knn)
        f_knn, _ = torch.max(f_knn,dim=2)

        global_feat = global_feat.unsqueeze(-1).repeat(1, 1, N ) # B, d, N
        feature = torch.cat((global_feat, local_feats, f_knn, points), dim=1)
        # out = torch.relu(self.mlp(feature)) # B, 1, N
        out = self.mlp(feature) # B, 1, N

        return out