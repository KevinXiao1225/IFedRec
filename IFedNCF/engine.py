import torch
from utils import *
import numpy as np
import copy
from data import UserItemRatingDataset
from torch.utils.data import DataLoader


class Engine(object):
    """Meta Engine for training & evaluating NCF model

    Note: Subclass should implement self.client_model and self.server_model!
    """

    def __init__(self, config):
        self.config = config  # model configuration 
        self.server_opt = torch.optim.Adam(self.server_model.parameters(), lr=config['lr_server'], # 服务器端优化器
                                           weight_decay=config['l2_regularization'])
        
        self.server_model_param = {} # 服务器端参数
        self.client_model_params = {} # 客户端参数
        
        self.client_crit = torch.nn.BCELoss() # 二元交叉熵损失 L_local
        self.server_crit = torch.nn.MSELoss() # 为均方误差损失 

    def instance_user_train_loader(self, user_train_data):
        """instance a user's train loader.""" # 实例化用户的训练数据加载器
        dataset = UserItemRatingDataset(user_tensor=torch.LongTensor(user_train_data[0]), # 来自data.py的类，使得数据便于被DataLoader加载
                                        item_tensor=torch.LongTensor(user_train_data[1]),
                                        target_tensor=torch.FloatTensor(user_train_data[2]))
        return DataLoader(dataset, batch_size=self.config['batch_size'], shuffle=True) # shuffle=True 在每个epoch（训练周期）开始时打乱数据

    def fed_train_single_batch(self, model_client, batch_data, optimizers):
        """train a batch and return an updated model.""" # 在用户端训练一个batch并更新
        # load batch data.
        _, items, ratings = batch_data[0], batch_data[1], batch_data[2] # _是一个常用的占位符变量名，表示这部分的值不会被使用
        ratings = ratings.float() # 便于计算
        reg_item_embedding = copy.deepcopy(self.server_model_param['global_item_rep']) # 服务器中复制全局item_embedding

        if self.config['use_cuda'] is True: 
            items, ratings = items.cuda(), ratings.cuda()
            reg_item_embedding = reg_item_embedding.cuda()

        optimizer, optimizer_u, optimizer_i = optimizers
        # update score function.
        # 联邦学习中不同的部分往往采取不同的优化器
        optimizer.zero_grad()
        optimizer_u.zero_grad()
        optimizer_i.zero_grad()
        ratings_pred = model_client(items)
        loss = self.client_crit(ratings_pred.view(-1), ratings)
        regularization_term = compute_regularization(model_client, reg_item_embedding) 
        loss += self.config['reg'] * regularization_term
        loss.backward()
        optimizer.step()
        optimizer_u.step()
        optimizer_i.step()
        return model_client

    def aggregate_clients_params(self, round_user_params, item_content):
        """receive client models' parameters in a round, aggregate them and store the aggregated result for server."""
        # aggregate item embedding and score function via averaged aggregation.
        t = 0
        for user in round_user_params.keys():
            # load a user's parameters.
            user_params = round_user_params[user]
            # print(user_params)
            if t == 0:
                self.server_model_param = copy.deepcopy(user_params)
            else:
                for key in user_params.keys():
                    self.server_model_param[key].data += user_params[key].data
            t += 1
        for key in self.server_model_param.keys():
            self.server_model_param[key].data = self.server_model_param[key].data / len(round_user_params)

        # train the item representation learning module. 元属性网络
        item_content = torch.tensor(item_content)
        target = self.server_model_param['embedding_item.weight'].data
        if self.config['use_cuda'] is True:
            item_content = item_content.cuda()
            target = target.cuda()
        self.server_model.train()
        for epoch in range(self.config['server_epoch']):
            self.server_opt.zero_grad()
            logit_rep = self.server_model(item_content)
            loss = self.server_crit(logit_rep, target)
            loss.backward()
            self.server_opt.step()

        # store the global item representation learned by server model.
        self.server_model.eval()
        # 在不计算梯度的情况下，通过服务器模型前向传播 item_content，得到全局物品表示。
        with torch.no_grad(): 
            global_item_rep = self.server_model(item_content)
        self.server_model_param['global_item_rep'] = global_item_rep


    def fed_train_a_round(self, user_ids, all_train_data, round_id, item_content):
        """train a round."""
        # sample users participating in single round.
        if self.config['clients_sample_ratio'] <= 1:
            num_participants = int(self.config['num_users'] * self.config['clients_sample_ratio'])
            participants = np.random.choice(user_ids, num_participants, replace=False)
        else:
            participants = np.random.choice(user_ids, self.config['clients_sample_num'], replace=False)

        # initialize server parameters for the first round.
        if round_id == 0:
            item_content = torch.tensor(item_content)
            if self.config['use_cuda'] is True:
                item_content = item_content.cuda()
            self.server_model.eval()
            with torch.no_grad():
                global_item_rep = self.server_model(item_content)
            self.server_model_param['global_item_rep'] = global_item_rep

        # store users' model parameters of current round.
        round_participant_params = {} # 初始化一个字典储存本轮参加更新的用户
        # perform model update for each participated user.
        for user in participants:
            # copy the client model architecture from self.client_model
            model_client = copy.deepcopy(self.client_model)
            # for the first round, client models copy initialized parameters directly.
            # for other rounds, client models receive updated item embedding from server.
            if round_id != 0:
                user_param_dict = copy.deepcopy(self.client_model.state_dict())
                if user in self.client_model_params.keys():
                    for key in self.client_model_params[user].keys():
                        user_param_dict[key] = copy.deepcopy(self.client_model_params[user][key].data).cuda()
                user_param_dict['embedding_item.weight'] = copy.deepcopy(self.server_model_param['embedding_item.weight'].data).cuda()
                model_client.load_state_dict(user_param_dict)
            # Defining optimizers
            # optimizer is responsible for updating score function.
            optimizer = torch.optim.SGD(
                [{"params": model_client.fc_layers.parameters()},
                 {"params": model_client.affine_output.parameters()}],
                lr=self.config['lr_client'])  # MLP optimizer
            # optimizer_u is responsible for updating user embedding.
            optimizer_u = torch.optim.SGD(model_client.embedding_user.parameters(),
                                          lr=self.config['lr_client'] / self.config['clients_sample_ratio'] * self.config[
                                              'lr_eta'] - self.config['lr_client'])  # User optimizer
            # optimizer_i is responsible for updating item embedding.
            optimizer_i = torch.optim.SGD(model_client.embedding_item.parameters(),
                                          lr=self.config['lr_client'] * self.config['num_items_train'] * self.config[
                                              'lr_eta'] -
                                             self.config['lr_client'])  # Item optimizer
            optimizers = [optimizer, optimizer_u, optimizer_i]

            # load current user's training data and instance a train loader.
            user_train_data = all_train_data[user]
            user_dataloader = self.instance_user_train_loader(user_train_data)
            model_client.train()
            # update client model.
            for epoch in range(self.config['local_epoch']):
                for batch_id, batch in enumerate(user_dataloader):
                    assert isinstance(batch[0], torch.LongTensor)
                    model_client = self.fed_train_single_batch(model_client, batch, optimizers)
            # obtain client model parameters.
            client_param = model_client.state_dict()
            # store client models' local parameters for personalization.
            self.client_model_params[user] = {}
            for key in client_param.keys():
                if key != 'embedding_item.weight':
                    self.client_model_params[user][key] = copy.deepcopy(client_param[key].data).cpu()
            # store client models' local parameters for global update.
            round_participant_params[user] = {}
            round_participant_params[user]['embedding_item.weight'] = copy.deepcopy(
                client_param['embedding_item.weight']).data.cpu()
        # aggregate client models in server side.
        self.aggregate_clients_params(round_participant_params, item_content)


    def fed_evaluate(self, evaluate_data, item_content, item_ids_map):
        """evaluate all client models' performance using testing data."""
        """input: 
        evaluate_data: (uid, iid) dataframe.
        item_content: evaluated item raw feature.
        item_ids_map: {ori_id: reindex_id} dict.
           output:
        recall, precision, ndcg
        """
        item_content = torch.tensor(item_content)
        if self.config['use_cuda'] is True:
            item_content = item_content.cuda()

        # obtain cold-start items' latent representation via server model.
        current_model = copy.deepcopy(self.server_model)
        current_model.eval()
        with torch.no_grad():
            item_rep = current_model(item_content)

        # obtain cola-start items' prediction for each user.
        user_ids = evaluate_data['uid'].unique()
        user_item_preds = {}
        for user in user_ids:
            user_model = copy.deepcopy(self.client_model)
            user_param_dict = copy.deepcopy(self.client_model.state_dict())
            if user in self.client_model_params.keys():
                for key in self.client_model_params[user].keys():
                    user_param_dict[key] = copy.deepcopy(self.client_model_params[user][key].data).cuda()
            user_model.load_state_dict(user_param_dict)
            user_model.eval()
            with torch.no_grad():
                cold_pred = user_model.cold_predict(item_rep)
                user_item_preds[user] = cold_pred.view(-1)

        # compute the evaluation metrics.
        recall, precision, ndcg = compute_metrics(evaluate_data, user_item_preds, item_ids_map, self.config['recall_k'])
        return recall, precision, ndcg