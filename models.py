import torch
import logging
import numpy as np
from itertools import repeat
from torch.autograd import Variable
import torch.nn.functional as F
from torch import nn
import graphLayer
from torchvision import transforms, utils
import os

class EmbeddingLayer(nn.Module):

    def __init__(self, nb_emb, emb_size=32):

        self.emb_size = emb_size
        super(EmbeddingLayer, self).__init__()

        # The embeddings
        self.emb_size = emb_size
        self.emb = nn.Parameter(torch.rand(nb_emb, emb_size))

    def forward(self, x):
        emb = x * self.emb
        return emb



class AttentionLayer(nn.Module):

    def __init__(self, in_dim):

        self.in_dim = in_dim
        super(AttentionLayer, self).__init__()

        # The view vector.
        self.attn = nn.Linear(self.in_dim, 1)
        self.temperature = 1.

    def forward(self, x):
        nb_examples, nb_channels, nb_nodes = x.size()
        x = x.permute(0, 2, 1).contiguous()  # from ex, ch, node -> ex, node, ch
        x = x.view(-1, nb_channels)

        # attn_weights = F.softmax(self.attn(x), dim=1)# Should be able to do that,
        # I have some problem with pytorch right now, so I'm doing i manually. Also between you and me, the pytorch example for attention sucks.
        attn_weights = torch.exp(self.attn(x)*self.temperature)
        attn_weights = attn_weights.view(nb_examples, nb_nodes, 1)
        attn_weights = attn_weights / attn_weights.sum(dim=1).unsqueeze(-1) # normalizing

        x = x.view(nb_examples, nb_nodes, nb_channels)
        attn_applied = x * attn_weights
        attn_applied = attn_applied.sum(dim=1)
        #print attn_weights[0].max()

        return attn_applied

class ElementwiseGateLayer(nn.Module):

    def __init__(self, id_dim):

        self.in_dim = id_dim
        super(ElementwiseGateLayer, self).__init__()

        self.attn = nn.Linear(self.in_dim, 1, bias=True)

    def forward(self, x):

        nb_examples, nb_nodes, nb_channels = x.size()
        x = x.view(-1, nb_channels)

        gate_weights = torch.sigmoid(self.attn(x))
        #tmp = torch.exp(self.attn(x))
        #gate_weights = (F.relu(tmp - 0.1) + 0.1) / (1 + tmp)

        gate_weights = gate_weights.view(nb_examples, nb_nodes, 1)

        return gate_weights

class StaticElementwiseGateLayer(nn.Module):

    def __init__(self, id_dim):

        self.in_dim = id_dim
        super(StaticElementwiseGateLayer, self).__init__()

        self.attn = nn.Parameter(torch.zeros(50), requires_grad=True) + 1.

    def forward(self, x):
        #attn = Variable(self.attn, requires_grad=True)

        nb_examples, nb_nodes, nb_channels = x.size()
        gate_weights = torch.sigmoid(self.attn)
        gate_weights = gate_weights.view(nb_nodes, 1)

        print gate_weights

        return gate_weights


def save_computations(self, input, output):
    setattr(self, "input", input)
    setattr(self, "output", output)

class SparseLogisticRegression(nn.Module):

    def __init__(self, nb_nodes, input_dim, adj, out_dim, on_cuda=True):

        super(SparseLogisticRegression, self).__init__()

        self.nb_nodes = nb_nodes
        self.input_dim = input_dim

        np.fill_diagonal(adj, 0.)
        D = adj.sum(0) + 1e-5
        laplacian = np.eye(D.shape[0]) - np.diag((D**-0.5)).dot(adj).dot(np.diag((D**-0.5)))

        self.laplacian = torch.FloatTensor(laplacian)
        self.out_dim = out_dim
        self.on_cuda = on_cuda

        # The logistic layer.
        logistic_in_dim = nb_nodes * input_dim
        logistic_layer = nn.Linear(logistic_in_dim, out_dim)
        logistic_layer.register_forward_hook(save_computations)  # For monitoring

        self.my_logistic_layers = nn.ModuleList([logistic_layer])  # A lsit to be consistant with the other layer.


    def forward(self, x):

        nb_examples, nb_nodes, nb_channels = x.size()
        x = x.view(nb_examples, -1)
        x = self.my_logistic_layers[-1](x)

        return x

    def regularization(self):

        laplacian = Variable(self.laplacian, requires_grad=False)
        if self.on_cuda:
            laplacian = laplacian.cuda()



        weight = self.my_logistic_layers[-1].weight
        reg = torch.abs(weight).mm(laplacian) * torch.abs(weight)
        return [reg.sum()]



class GraphNetwork(nn.Module):

    def __init__(self, nb_nodes, input_dim, channels, adj, out_dim,
                 on_cuda=True, add_emb=None, transform_adj=None, agregate_adj=None, graphLayerType=graphLayer.CGNLayer, use_gate=0.0001, dropout=False):
        super(GraphNetwork, self).__init__()

        if transform_adj is None:
            transform_adj = []

        self.my_layers = []
        self.out_dim = out_dim
        self.on_cuda = on_cuda
        self.nb_nodes = nb_nodes
        self.nb_channels = channels
        self.add_emb = add_emb
        self.graphLayerType = graphLayerType
        self.agregate_adj = agregate_adj
        self.dropout = dropout


        if add_emb:
            logging.info("Adding node embeddings.")
            self.emb = EmbeddingLayer(nb_nodes, add_emb)
            self.emb.register_forward_hook(save_computations) # For monitoring
            input_dim = self.emb.emb_size

        # The graph convolutional layers
        convs = []
        dims = [input_dim] + channels
        self.dims = dims
        for i, [c_in, c_out] in enumerate(zip(dims[:-1], dims[1:])):
            # transformation to apply at each layer.

            #transform_tmp = transforms.Compose([foo(please_ignore=i == 0, unique_id=i) for foo in transform_adj])
            #transform_tmp = None
            #agregate_tmp = None
            #if transform_adj is not None:
            #    transform_tmp = transform_adj(i)
            #if agregate_adj is not None:
            #    agregate_tmp = agregate_adj(i)

            layer = graphLayerType(adj, c_in, c_out, on_cuda, i, transform_adj=transform_adj, agregate_adj=agregate_adj)
            layer.register_forward_hook(save_computations) # For monitoring
            convs.append(layer)

            adj = convs[-1].adj

        self.my_convs = nn.ModuleList(convs)

        # The logistic layer
        logistic_layer = []
        logistic_in_dim = [nb_nodes * dims[-1]]

        for d in logistic_in_dim:
            layer = nn.Linear(d, out_dim)
            layer.register_forward_hook(save_computations)  # For monitoring
            logistic_layer.append(layer)

        self.my_logistic_layers = nn.ModuleList(logistic_layer)


        self.use_gate = use_gate

        if use_gate > 0.:
            gates = []
            for c_in in dims[1:]:
                gate = ElementwiseGateLayer(c_in)
                gate.register_forward_hook(save_computations)  # For monitoring
                gates.append(gate)

            self.gates = nn.ModuleList(gates)

        else:
            self.gates = [None] * (len(dims) - 1)

        self.my_dropouts = [None] * (len(dims) - 1)
        if dropout:
            print "Doing drop-out"
            self.my_dropouts = nn.ModuleList([torch.nn.Dropout(int(dropout)*min(id_layer / 10., 0.5)) for id_layer in range(len(dims)-1)])

        logging.info("Done!")

        # TODO: add all the funky bells and stuff that the old CGN has.

    def forward(self, x):

        nb_examples, nb_nodes, nb_channels = x.size()
        if self.add_emb:
            x = self.emb(x)


        last_g = None
        for i, [layer, gate, dropout] in enumerate(zip(self.my_convs, self.gates, self.my_dropouts)):

            old_x = x

            if self.use_gate > 0.:

                x = layer(x)
                g = gate(x)

                if last_g is None:
                    last_g = g
                else:
                    last_g = last_g * g

                x = g * x
            else:
                x = layer(x)

            x = F.relu(x)# + old_x

            if dropout is not None:
                id_to_keep = dropout(torch.FloatTensor(np.ones((x.size(0), x.size(1))))).unsqueeze(2)

                if self.on_cuda:
                    id_to_keep = id_to_keep.cuda()

                x = x * id_to_keep

        x = self.my_logistic_layers[-1](x.view(nb_examples, -1))

        return x

    def regularization(self):

        return []
        # reg = 0.
        #
        # if self.use_gate > 0.:
        #     for g in self.gates:
        #         reg += ((g.output.view(-1) -.5)**2).sum() # got from register_forward_hook
        #
        # return  [torch.FloatTensor(0.)] #- reg * self.use_gate


    def get_representation(self):

        # TODO: There is a more systematic way to do that with self.named_children or something, but for that we have
        # to refactor the code first.

        def add_rep(layer, name, rep):
            rep[name] = {'input': layer.input[0].cpu().data.numpy(), 'output': layer.output.cpu().data.numpy()}

        representation = {}
        #import ipdb; ipdb.set_trace()

        if self.add_emb:
            add_rep(self.emb, 'emb', representation)

        for i, [layer, gate] in enumerate(zip(self.my_convs, self.gates)):

            if self.use_gate > 0.:
                add_rep(layer, 'layer_{}'.format(i), representation)
                add_rep(gate, 'gate_{}'.format(i), representation)

            else:
                add_rep(layer, 'layer_{}'.format(i), representation)

        add_rep(self.my_logistic_layers[-1], 'logistic', representation)
        return representation
# Create a module for the CGN:
class CGN(GraphNetwork):

    def __init__(self, **kwargs):
        super(CGN, self).__init__(graphLayerType=graphLayer.CGNLayer, **kwargs)

# Create a module for the SGC:
class SGC(GraphNetwork):

    def __init__(self, **kwargs):
        super(SGC, self).__init__(graphLayerType=graphLayer.SGCLayer, **kwargs)

# Create a module for the LCG:
class LCG(GraphNetwork):
    def __init__(self, **kwargs):
        super(LCG, self).__init__(graphLayerType=graphLayer.LCGLayer, **kwargs)

# Create a module for MLP
class MLP(nn.Module):
    def __init__(self, input_dim, channels, out_dim=None, on_cuda=True, dropout=False):
        super(MLP, self).__init__()

        self.my_layers = []
        self.out_dim = out_dim
        self.on_cuda = on_cuda
        self.dropout = dropout

        dims = [input_dim] + channels

        logging.info("Constructing the network...")
        layers = []
        for c_in, c_out in zip(dims[:-1], dims[1:]):
            layer = nn.Linear(c_in, c_out)
            layers.append(layer)
        self.my_layers = nn.ModuleList(layers)

        if channels:
            self.last_layer = nn.Linear(channels[-1], out_dim)
        else:
            self.last_layer = nn.Linear(input_dim, out_dim)

        self.my_dropout = None
        if dropout:
            print "Doing Drop-out"
            self.my_dropout = torch.nn.Dropout(0.5)

        logging.info("Done!")

    def forward(self, x):
        nb_examples, nb_nodes, nb_channels = x.size()

        x = x.permute(0, 2, 1).contiguous()  # from ex, node, ch, -> ex, ch, node
        for layer in self.my_layers:
            x = F.relu(layer(x.view(nb_examples, -1)))  # or relu, sigmoid...

            if self.dropout:
                #import ipdb; ipdb.set_trace()
                x = self.my_dropout(x)

        x = self.last_layer(x.view(nb_examples, -1))

        return x

    def regularization(self):
        return []

# Create a module for MLP
class Random(nn.Module):
    def __init__(self, input_dim, channels, out_dim=None, on_cuda=True):
        super(Random, self).__init__()

        self.my_layers = []
        self.out_dim = out_dim
        self.on_cuda = on_cuda

        dims = [input_dim] + channels

        logging.info("Constructing the network...")
        layers = []
        for c_in, c_out in zip(dims[:-1], dims[1:]):
            layer = nn.Linear(c_in, c_out)
            layers.append(layer)
        self.my_layers = nn.ModuleList(layers)

        if channels:
            self.last_layer = nn.Linear(channels[-1], out_dim)
        else:
            self.last_layer = nn.Linear(input_dim, out_dim)

        logging.info("Done!")

    def forward(self, x):
        nb_examples, nb_nodes, nb_channels = x.size()
        guesses = [np.random.permutation(x) for x in repeat(range(self.out_dim), nb_examples)]
        x = Variable(torch.cuda.FloatTensor(guesses))
        return x

    def regularization(self):
        return []


class CNN(nn.Module):
    def __init__(self, input_dim, channels, grid_shape, out_dim=None, on_cuda=True):
        super(CNN, self).__init__()

        self.input_dim = input_dim
        self.channels = channels
        self.out_dim = out_dim
        self.grid_shape = grid_shape
        kernel_size = 2
        stride = 1
        padding = 0

        layers = []
        dims = [input_dim] + channels

        current_size = grid_shape[0]

        for c_in, c_out in zip(dims[:-1], dims[1:]):
            layer = nn.Sequential(
                nn.Conv2d(c_in, c_out, kernel_size=kernel_size, padding=padding, stride=stride),
                # nn.BatchNorm2d(16), # True that maybe?
                nn.ReLU(),
                #nn.MaxPool2d(2)
            )

            layers.append(layer)

            new_size = np.ceil((current_size + 2*padding - (kernel_size -1))/float(stride))
            print current_size, new_size
            current_size = int(new_size)

        self.my_layers = nn.ModuleList(layers)
        out = current_size * current_size * dims[-1]
        self.fc = nn.Linear(out, out_dim)

    def forward(self, x):


        # Reshape
        x = x.view(-1, 1, self.grid_shape[0], self.grid_shape[1])

        # The conv.
        out = x
        for layer in self.my_layers:
            out = layer(out)

        #import ipdb; ipdb.set_trace()


        # fully connected.
        out = out.view(out.size(0), -1)
        out = self.fc(out)
        return out

    def regularization(self):
        return []

def get_model(opt, dataset):
    """
    Return a model based on the options.
    :param opt:
    :param dataset:
    :param nb_class:
    :return:
    """

    model = opt.model
    num_channel = opt.num_channel
    num_layer = opt.num_layer
    on_cuda = opt.cuda

    adj_transform, agregate_function = graphLayer.get_transform(opt, dataset.get_adj())

    if model == 'cgn':
        # To have a feel of the model, please take a look at cgn.ipynb
        my_model = CGN(nb_nodes=dataset.nb_nodes, input_dim=1, channels=[num_channel] * num_layer, adj=dataset.get_adj(), out_dim=dataset.nb_class,
                       on_cuda=on_cuda, add_emb=opt.use_emb,
                       transform_adj=adj_transform,agregate_adj=agregate_function, use_gate=opt.use_gate, dropout=opt.dropout)  # TODO: add a bunch of the options

    elif model == 'lcg':

        my_model = LCG(nb_nodes=dataset.nb_nodes, input_dim=1, channels=[num_channel] * num_layer, adj=dataset.get_adj(), out_dim=dataset.nb_class,
                       on_cuda=on_cuda, add_emb=opt.use_emb,
                       transform_adj=adj_transform,agregate_adj=agregate_function, use_gate=opt.use_gate, dropout=opt.dropout)   # TODO: add a bunch of the options

    elif model == 'sgc':
        my_model = SGC(nb_nodes=dataset.nb_nodes, input_dim=1, channels=[num_channel] * num_layer, adj=dataset.get_adj(), out_dim=dataset.nb_class,
                       on_cuda=on_cuda, add_emb=opt.use_emb,
                       transform_adj=adj_transform,agregate_adj=agregate_function, use_gate=opt.use_gate, dropout=opt.dropout)   # TODO: add a bunch of the options

    elif model == 'slr':
        #nb_nodes, input_dim, adj, out_dim, on_cuda=True):
        my_model = SparseLogisticRegression(nb_nodes=dataset.nb_nodes, input_dim=1, adj=dataset.get_adj(), out_dim=dataset.nb_class, on_cuda=on_cuda)  # TODO: add a bunch of the options

    elif model == 'mlp':
        my_model = MLP(dataset.nb_nodes, [num_channel] * num_layer, dataset.nb_class, on_cuda=on_cuda, dropout=opt.dropout)  # TODO: add a bunch of the options

    elif model == 'random':
        my_model = Random(dataset.nb_nodes, [num_channel] * num_layer, dataset.nb_class, on_cuda=on_cuda)

    elif model == 'cnn':
        assert opt.dataset == 'percolate'
        # TODO: to change the shape.
        #import ipdb; ipdb.set_trace()

        grid_shape = int(np.sqrt(dataset.get_adj().shape[0])) # for now we
        grid_shape = [grid_shape, grid_shape]

        my_model = CNN(input_dim=1, channels=[num_channel] * num_layer, grid_shape=grid_shape, out_dim=dataset.nb_class, on_cuda=on_cuda)

    else:
        raise ValueError

    return my_model

def setup_l1_loss(my_model, l1_loss_lambda, l1_criterion, on_cuda):
    l1_loss = 0
    if hasattr(my_model, 'my_logistic_layers'):
        l1_loss += calculate_l1_loss(my_model.my_logistic_layers.parameters(), l1_loss_lambda, l1_criterion, on_cuda)
    if hasattr(my_model, 'my_layers') and len(my_model.my_layers) > 0 and type(my_model.my_layers[0]) == torch.nn.modules.linear.Linear:
        l1_loss += calculate_l1_loss(my_model.my_layers[0].parameters(), l1_loss_lambda, l1_criterion, on_cuda)
    if hasattr(my_model, 'last_layer') and type(my_model.last_layer) == torch.nn.modules.linear.Linear:
        l1_loss += calculate_l1_loss(my_model.last_layer.parameters(), l1_loss_lambda, l1_criterion, on_cuda)
    return l1_loss


def calculate_l1_loss(param_generator, l1_loss_lambda, l1_criterion, on_cuda):
    l1_loss = 0
    for param in param_generator:
        if on_cuda:
            l1_target = Variable(torch.FloatTensor(param.size()).zero_()).cuda()
        else:
            l1_target = Variable(torch.FloatTensor(param.size()).zero_())
        l1_loss += l1_criterion(param, l1_target)
    return l1_loss * l1_loss_lambda


# # Create a module for the CGN:
# #TODO: refactor LCG, CGN and CGN, they are pretty much all the same, should make a super class GraphNetwork.
# # Then we would only need the add the bells and wishle at only one place.
#
# class CGN(nn.Module):
#
#     def __init__(self, nb_nodes, input_dim, channels, adj, out_dim,
#                  on_cuda=True, add_residual=False, attention_layer=0, add_emb=None, transform_adj=None, agregate_adj=None):
#         super(CGN, self).__init__()
#
#         if transform_adj is None:
#             transform_adj = []
#
#         if agregate_adj is None:
#             agregate_adj = []
#
#         self.my_layers = []
#         self.out_dim = out_dim
#         self.on_cuda = on_cuda
#         self.add_residual = add_residual
#         self.nb_nodes = nb_nodes
#         self.nb_channels = channels
#         self.attention_layer = attention_layer
#         self.add_emb = add_emb
#         self.agregate_adj = agregate_adj
#
#         if add_emb:
#             print "Adding node embeddings."
#             self.emb = EmbeddingLayer(nb_nodes, add_emb)
#             input_dim = self.emb.emb_size
#
#         dims = [input_dim] + channels
#
#         print "Constructing the network..."
#         # The normal layer
#         layers = []
#         for c_in, c_out in zip(dims[:-1], dims[1:]):
#             layer = nn.Conv1d(c_in, c_out, 1, bias=True)
#             layers.append(layer)
#         self.my_layers = nn.ModuleList(layers)
#
#         # The convolutional layer
#         convs = []
#
#         for i in range(len(channels)):
#             # transformation to apply at each layer.
#             transform_tmp = transforms.Compose([foo(please_ignore=i == 0, unique_id=i) for foo in transform_adj])
#             convs.append(graphLayer.CGNLayer(nb_nodes, adj, on_cuda, transform_adj=transform_tmp, agregate_adj=agregate_adj))
#             adj = convs[-1].adj
#
#         self.my_convs = nn.ModuleList(convs)
#
#         # The logistic layer
#         logistic_layer = []
#         if not channels: # Only have one layer
#             logistic_in_dim = [nb_nodes * input_dim]
#         elif not add_residual: # Adding a final logistic regression.
#             if attention_layer > 0:
#                 logistic_in_dim = [channels[-1] * attention_layer]  # Changed
#             else:
#                 logistic_in_dim = [nb_nodes * channels[-1]] # Changed here
#         else:
#             print "Adding skip connections..."
#             if attention_layer > 0:
#                 logistic_in_dim = [d * nb_nodes for d in dims]
#             else:
#                 logistic_in_dim = [d * attention_layer for d in dims]
#
#         for d in logistic_in_dim:
#             layer = nn.Linear(d, out_dim)
#             logistic_layer.append(layer)
#
#         self.my_logistic_layers = nn.ModuleList(logistic_layer)
#         logging.info("Done!")
#
#         if attention_layer > 0:
#             print "Adding {} attentions layer.".format(attention_layer)
#             self.att = nn.ModuleList([AttentionLayer(channels[-1])] * attention_layer)
#
#     def forward(self, x):
#
#         out = None
#         nb_examples, nb_nodes, nb_channels = x.size()
#         if self.add_emb:
#             x = self.emb(x)
#
#         x = x.permute(0, 2, 1).contiguous()# from ex, node, ch, -> ex, ch, node
#
#         # Do graph convolution for all
#         for num, [conv, layer] in enumerate(zip(self.my_convs, self.my_layers)):
#
#             if self.add_residual: # skip connection
#                 if out is None:
#                     out = self.my_logistic_layers[num](x.view(nb_examples, -1))
#                 else:
#                     out += self.my_logistic_layers[num](x.view(nb_examples, -1))
#
#             x = conv(x) # conv
#             x = F.relu(layer(x))  # or relu, sigmoid...
#
#         # agregate the attention on the last layer.
#         if self.attention_layer > 0:
#             x = torch.stack([att(x) for att in self.att], dim=-1)
#
#         if out is None:
#             out = self.my_logistic_layers[-1](x.view(nb_examples, -1))
#         else:
#             out += self.my_logistic_layers[-1](x.view(nb_examples, -1))
#
#         return out



#
# #spectral graph conv
# class SGC(nn.Module):
#     def __init__(self,input_dim, A, channels=1, out_dim=2, on_cuda=False, num_layers = 1, arg_max = -200):
#         super(SGC, self).__init__()
#
#         print "Bip bop I'm Francis and I'm lazy, I need to use all the adjs."
#         A = A[0] # just use first graph
#
#         self.my_layers = []
#         self.out_dim = out_dim
#         self.on_cuda = on_cuda
#         self.nb_nodes = input_dim
#         self.num_layers = num_layers
#
#         self.channels = 1#channels
#         #dims = [input_dim] + channels
#
#         def if_cuda(x):
#             return x.cuda() if self.on_cuda else x
#
#         print "Constructing the eigenvectors..."
#
#         D = np.diag(A.sum(axis=1))
#         self.L = D-A
#         self.L = torch.FloatTensor(self.L)
#         self.L = if_cuda(self.L)
#
#         eg = load_eigenvectors("",self.L)
#         if eg != None:
#             self.g, self.V = if_cuda(eg[0]),if_cuda(eg[1])
#         else:
#             self.g, self.V = torch.eig(self.L, eigenvectors=True)
#             save_eigenvectors("",self.L, self.g, self.V)
#
#         self.V = if_cuda(self.V.cpu().half())
#         self.g = if_cuda(self.g.cpu().half())
#
#         print "self.nb_nodes", self.nb_nodes
#         self.F = nn.Parameter(if_cuda(torch.rand(self.nb_nodes, self.nb_nodes).half()), requires_grad=True)
#         self.my_bias = nn.Parameter(if_cuda(torch.zeros(self.nb_nodes, channels)), requires_grad=True)
#
#
#         last_layer = nn.Linear(self.nb_nodes * self.channels, out_dim).half()
#         self.my_logistic_layers = nn.ModuleList([last_layer])
#
#         logging.info("Done!")
#
#     def forward(self, x):
#
#         nb_examples, nb_nodes, nb_channels = x.size()
#
#         def if_cuda(x):
#             return x.cuda().half() if self.on_cuda else x.half()
#
#         x = if_cuda(x.cpu())
#         Vx = torch.matmul(torch.transpose(Variable(self.V), 0,1),x)
#         FVx = torch.matmul(self.F, Vx)
#         VFVx = torch.matmul(Variable(self.V),FVx)
#         x = VFVx
#
#
#         x = self.my_logistic_layers[-1](x.view(nb_examples, -1))
#         x = F.softmax(x, dim=1)
#
#         return x
#
#
# def get_eigenvectors_filename(name,L):
#     cachepath="./cache/"
#     matrix_hash=str(hash(L.cpu().numpy().tostring()))
#     return cachepath + matrix_hash + ".npz"
#
# def load_eigenvectors(name,L):
#     filename = get_eigenvectors_filename(name,L)
#     if os.path.isfile(filename):
#         print "loading", filename
#         eg = np.load(open(filename))
#         return (torch.FloatTensor(eg["g"]),torch.FloatTensor(eg["V"]))
#
# def save_eigenvectors(name,L,g,V):
#     filename = get_eigenvectors_filename(name,L)
#     print "saving", filename
#     return np.savez(open(filename,'w+'),g=g.cpu().numpy(),V=V.cpu().numpy())
