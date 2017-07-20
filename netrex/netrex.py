import numpy as np

import torch

import torch.nn as nn
import torch.nn.functional as F

import torch.optim as optim

from torch.autograd import Variable

from netrex.layers import ScaledEmbedding, ZeroEmbedding


def _gpu(tensor, gpu=False):

    if gpu:
        return tensor.cuda()
    else:
        return tensor


def _cpu(tensor):

    if tensor.is_cuda:
        return tensor.cpu()
    else:
        return tensor


def _minibatch(tensor, batch_size):

    for i in range(0, len(tensor), batch_size):
        yield tensor[i:i + batch_size]


def _generate_sequences(interactions, max_sequence_length):

    interactions = interactions.tocsr()

    indptr = interactions.indptr
    data = interactions.data

    for row_num in range(interactions.shape[0]):

        row_data = data[indptr[row_num]:indptr[row_num + 1]]

        if not len(row_data):
            continue

        for (seq, target) in zip(_minibatch(row_data, max_sequence_length),
                                 _minibatch(row_data, max_sequence_length)):

            yield seq[:-1], target


def generate_sequences(interactions, max_sequence_length=20):
    """
    Generate padded sequences from a CSR matrix of interactions
    where columns are timestamps and values are item ids.
    """

    num_subsequences = sum(1 for x in _generate_sequences(interactions,
                                                          max_sequence_length))

    sequences = np.zeros((num_subsequences, max_sequence_length), dtype=np.int64)
    targets = np.zeros((num_subsequences, max_sequence_length), dtype=np.int64)

    for i, (seq, target) in enumerate(_generate_sequences(interactions,
                                                          max_sequence_length)):

        if not len(seq):
            continue

        sequences[i][-len(seq):] = seq
        targets[i][-len(target):] = target

    return sequences, targets


class BilinearNet(nn.Module):

    def __init__(self, num_users, num_items, embedding_dim, sparse=False):
        super().__init__()

        self.embedding_dim = embedding_dim

        self.user_embeddings = ScaledEmbedding(num_users, embedding_dim,
                                               sparse=sparse)
        self.item_embeddings = ScaledEmbedding(num_items, embedding_dim,
                                               sparse=sparse)
        self.user_biases = ZeroEmbedding(num_users, 1, sparse=sparse)
        self.item_biases = ZeroEmbedding(num_items, 1, sparse=sparse)

    def forward(self, user_ids, item_ids):

        user_embedding = self.user_embeddings(user_ids)
        item_embedding = self.item_embeddings(item_ids)

        user_embedding = user_embedding.view(-1, self.embedding_dim)
        item_embedding = item_embedding.view(-1, self.embedding_dim)

        user_bias = self.user_biases(user_ids).view(-1, 1)
        item_bias = self.item_biases(item_ids).view(-1, 1)

        dot = (user_embedding * item_embedding).sum(1)

        return dot + user_bias + item_bias


class TruncatedBilinearNet(nn.Module):

    def __init__(self, num_users, num_items, embedding_dim, sparse=False):
        super().__init__()

        self.embedding_dim = embedding_dim

        self.rating_net = BilinearNet(num_users, num_items,
                                      embedding_dim, sparse=sparse)
        self.observed_net = BilinearNet(num_users, num_items,
                                        embedding_dim, sparse=sparse)

        self.stddev = nn.Embedding(1, 1)

    def forward(self, user_ids, item_ids):

        observed = F.sigmoid(self.observed_net(user_ids, item_ids))
        rating = self.rating_net(user_ids, item_ids)
        stddev = self.stddev((user_ids < -1).long()).view(-1, 1)

        return observed, rating, stddev


class LSTMNet(nn.Module):

    def __init__(self, num_items, embedding_dim, sparse=False):
        super().__init__()

        self.embedding_dim = embedding_dim

        self.item_embeddings = ScaledEmbedding(num_items, embedding_dim,
                                               sparse=sparse,
                                               padding_idx=0)
        self.item_biases = ZeroEmbedding(num_items, 1, sparse=sparse,
                                         padding_idx=0)

        self.lstm = nn.LSTM(batch_first=True,
                            input_size=embedding_dim,
                            hidden_size=embedding_dim)

    def forward(self, item_sequences, item_ids):

        target_embedding = self.item_embeddings(item_ids)
        user_representations, _ = self.lstm(
            self.item_embeddings(item_sequences)
        )
        target_bias = self.item_biases(item_ids)

        dot = (user_representations * target_embedding).sum(2)

        return dot + target_bias


class PoolNet(nn.Module):

    def __init__(self, num_items, embedding_dim, sparse=False):
        super().__init__()

        self.embedding_dim = embedding_dim

        self.item_embeddings = ScaledEmbedding(num_items, embedding_dim,
                                               sparse=sparse,
                                               padding_idx=0)
        self.item_biases = ZeroEmbedding(num_items, 1, sparse=sparse,
                                         padding_idx=0)

    def forward(self, item_sequences, item_ids):

        target_embedding = self.item_embeddings(item_ids)
        seen_embeddings = self.item_embeddings(item_sequences)
        user_representations = torch.cumsum(
            seen_embeddings,
            1
        )

        target_bias = self.item_biases(item_ids)

        dot = (user_representations * target_embedding).sum(2)

        return dot + target_bias


class PopularityNet(nn.Module):

    def __init__(self, num_items, sparse=False):
        super().__init__()

        self.item_biases = ZeroEmbedding(num_items, 1, sparse=sparse,
                                         padding_idx=0)

    def forward(self, item_sequences, item_ids):

        target_bias = self.item_biases(item_ids)

        return target_bias


class FactorizationModel(object):
    """
    A number of classic factorization models, implemented in PyTorch.

    Available loss functions:
    - pointwise logistic
    - BPR: Rendle's personalized Bayesian ranking
    - adaptive: a variant of WARP with adaptive selection of negative samples
    - regression: minimizing the regression loss between true and predicted ratings
    - truncated_regression: truncated regression model, that jointly models
                            the likelihood of a rating being given and the value
                            of the rating itself.

    Performance notes: neural network toolkits do not perform well on sparse tasks
    like recommendations. To achieve acceptable speed, either use the `sparse` option
    on a CPU or use CUDA with very big minibatches (1024+).
    """

    def __init__(self,
                 loss='pointwise',
                 embedding_dim=64,
                 n_iter=3,
                 batch_size=64,
                 l2=0.0,
                 use_cuda=False,
                 sparse=False):

        assert loss in ('pointwise',
                        'bpr',
                        'adaptive',
                        'regression',
                        'truncated_regression')

        self._loss = loss
        self._embedding_dim = embedding_dim
        self._n_iter = n_iter
        self._batch_size = batch_size
        self._l2 = l2
        self._use_cuda = use_cuda
        self._sparse = sparse

        self._num_users = None
        self._num_items = None
        self._net = None

    def _pointwise_loss(self, users, items, ratings):

        negatives = Variable(
            _gpu(
                torch.from_numpy(np.random.randint(0,
                                                   self._num_items,
                                                   len(users))),
                self._use_cuda)
        )

        positives_loss = (1.0 - F.sigmoid(self._net(users, items)))
        negatives_loss = F.sigmoid(self._net(users, negatives))

        return torch.cat([positives_loss, negatives_loss]).mean()

    def _bpr_loss(self, users, items, ratings):

        negatives = Variable(
            _gpu(
                torch.from_numpy(np.random.randint(0,
                                                   self._num_items,
                                                   len(users))),
                self._use_cuda)
        )

        return (1.0 - F.sigmoid(self._net(users, items) -
                                self._net(users, negatives))).mean()

    def _adaptive_loss(self, users, items, ratings,
        n_neg_candidates=5):
        negatives = Variable(
            _gpu(
                torch.from_numpy(
                    np.random.randint(0, self._num_items,
                        (len(users), n_neg_candidates))),
                self._use_cuda)
        )
        negative_predictions = self._net(
            users.repeat(n_neg_candidates, 1).transpose_(0,1),
            negatives
            ).view(-1, n_neg_candidates)

        best_negative_prediction, _ = negative_predictions.max(1)
        positive_prediction = self._net(users, items)

        return torch.mean(torch.clamp(best_negative_prediction -
                                      positive_prediction
                                      + 1.0, 0.0))

    def _truncated_regression_loss(self, users, items, ratings):

        negatives = Variable(
            _gpu(
                torch.from_numpy(np.random.randint(0,
                                                   self._num_items,
                                                   len(users))),
                self._use_cuda)
        )

        pos_prob, pos_rating, pos_stddev = self._net(users, items)

        positives_likelihood = (torch.log(pos_prob)
                                - 0.5 * torch.log(pos_stddev ** 2)
                                - (0.5 * (pos_rating - ratings) ** 2
                                   / (pos_stddev ** 2)))
        neg_prob, _, _ = self._net(users, negatives)
        negatives_likelihood = torch.log(1.0 - neg_prob)

        return torch.cat([-positives_likelihood, -negatives_likelihood]).mean()

    def _regression_loss(self, users, items, ratings):

        predicted_rating = self._net(users, items)

        return ((ratings - predicted_rating) ** 2).mean()

    def _shuffle(self, interactions):

        users = interactions.row
        items = interactions.col
        ratings = interactions.data

        shuffle_indices = np.arange(len(users))
        np.random.shuffle(shuffle_indices)

        return (users[shuffle_indices].astype(np.int64),
                items[shuffle_indices].astype(np.int64),
                ratings[shuffle_indices].astype(np.float32))

    def fit(self, interactions, verbose=False):
        """
        Fit the model.

        Arguments
        ---------

        interactions: np.float32 coo_matrix of shape [n_users, n_items]
             the matrix containing
             user-item interactions. The entries can be binary
             (for implicit tasks) or ratings (for regression
             and truncated regression).
        verbose: Bool, optional
             Whether to print epoch loss statistics.
        """

        self._num_users, self._num_items = interactions.shape

        if self._loss in ('truncated_regression',):
            self._net = _gpu(
                TruncatedBilinearNet(self._num_users,
                                     self._num_items,
                                     self._embedding_dim,
                                     sparse=self._sparse),
                self._use_cuda
            )
        else:
            self._net = _gpu(
                BilinearNet(self._num_users,
                            self._num_items,
                            self._embedding_dim,
                            sparse=self._sparse),
                self._use_cuda
            )

        if self._sparse:
            optimizer = optim.Adagrad(self._net.parameters(),
                                      weight_decay=self._l2)
        else:
            optimizer = optim.Adam(self._net.parameters(),
                                   weight_decay=self._l2)

        if self._loss == 'pointwise':
            loss_fnc = self._pointwise_loss
        elif self._loss == 'bpr':
            loss_fnc = self._bpr_loss
        elif self._loss == 'regression':
            loss_fnc = self._regression_loss
        elif self._loss == 'truncated_regression':
            loss_fnc = self._truncated_regression_loss
        else:
            loss_fnc = self._adaptive_loss

        for epoch_num in range(self._n_iter):

            users, items, ratings = self._shuffle(interactions)

            user_ids_tensor = _gpu(torch.from_numpy(users),
                                   self._use_cuda)
            item_ids_tensor = _gpu(torch.from_numpy(items),
                                   self._use_cuda)
            ratings_tensor = _gpu(torch.from_numpy(ratings),
                                  self._use_cuda)

            epoch_loss = 0.0

            for (batch_user,
                 batch_item,
                 batch_ratings) in zip(_minibatch(user_ids_tensor,
                                                  self._batch_size),
                                       _minibatch(item_ids_tensor,
                                                  self._batch_size),
                                       _minibatch(ratings_tensor,
                                                  self._batch_size)):

                user_var = Variable(batch_user)
                item_var = Variable(batch_item)
                ratings_var = Variable(batch_ratings)

                optimizer.zero_grad()

                loss = loss_fnc(user_var, item_var, ratings_var)
                epoch_loss += loss.data[0]

                loss.backward()
                optimizer.step()

            if verbose:
                print('Epoch {}: loss {}'.format(epoch_num, epoch_loss))

    def predict(self, user_ids, item_ids, ratings=False):
        """
        Compute the recommendation score for user-item pairs.

        Arguments
        ---------

        user_ids: integer or np.int32 array of shape [n_pairs,]
             single user id or an array containing the user ids for the user-item pairs for which
             a prediction is to be computed
        item_ids: np.int32 array of shape [n_pairs,]
             an array containing the item ids for the user-item pairs for which
             a prediction is to be computed.
        ratings: bool, optional
             Return predictions on ratings (rather than likelihood of rating)
        """

        if ratings:
            if self._loss not in ('regression',
                                  'truncated_regression'):
                raise ValueError('Ratings can only be returned '
                                 'when the truncated regression loss '
                                 'is used')

        user_ids = torch.from_numpy(user_ids.reshape(-1, 1).astype(np.int64))
        item_ids = torch.from_numpy(item_ids.reshape(-1, 1).astype(np.int64))

        user_var = Variable(_gpu(user_ids, self._use_cuda))
        item_var = Variable(_gpu(item_ids, self._use_cuda))

        out = self._net(user_var, item_var)

        if self._loss in ('truncated_regression',):
            if ratings:
                return _cpu((out[1]).data).numpy().flatten()
            else:
                return _cpu((out[0]).data).numpy().flatten()
        else:
            return _cpu(out.data).numpy().flatten()


class SequenceModel(object):
    """
    One-ahead prediction model.

    Can use one of the following user representations:
    - pool: pooling over previous items
    - lstm: LSTM over previous items
    - popularity: always predict the most popular item

    Can use one of the following losses
    - pointwise
    - BPR
    - adaptive
    """

    def __init__(self,
                 loss='pointwise',
                 representation='lstm',
                 embedding_dim=64,
                 n_iter=3,
                 batch_size=64,
                 l2=0.0,
                 use_cuda=False,
                 sparse=False):

        assert loss in ('pointwise',
                        'bpr',
                        'adaptive')

        assert representation in ('pool',
                                  'lstm',
                                  'popularity')

        self._loss = loss
        self._representation = representation
        self._embedding_dim = embedding_dim
        self._n_iter = n_iter
        self._batch_size = batch_size
        self._l2 = l2
        self._use_cuda = use_cuda
        self._sparse = sparse

        self._num_items = None
        self._net = None

    def _pointwise_loss(self, users, items, ratings):

        negatives = Variable(
            _gpu(
                torch.from_numpy(np.random.randint(0,
                                                   self._num_items,
                                                   tuple(users.size()))),
                self._use_cuda)
        )

        mask = (items > 0).float()

        positives_loss = (1.0 - F.sigmoid(self._net(users, items))) * mask
        negatives_loss = F.sigmoid(self._net(users, negatives)) * mask

        return torch.cat([positives_loss, negatives_loss]).mean()

    def _bpr_loss(self, users, items, ratings):

        negatives = Variable(
            _gpu(
                torch.from_numpy(np.random.randint(0,
                                                   self._num_items,
                                                   tuple(users.size()))),
                self._use_cuda)
        )

        mask = (items > 0).float()

        return ((1.0 - F.sigmoid(self._net(users, items) -
                                 self._net(users, negatives))) * mask).mean()

    def _adaptive_loss(self, users, items, ratings,
        n_neg_candidates=5):

        negative_predictions = []

        for _ in range(n_neg_candidates):
            negatives = Variable(
                _gpu(
                    torch.from_numpy(np.random.randint(0,
                                                       self._num_items,
                                                       tuple(users.size()))),
                    self._use_cuda)
            )

            negative_predictions.append(self._net(users, negatives))

        best_negative_prediction, _ = torch.cat(negative_predictions, 2).max(2)
        positive_prediction = self._net(users, items)

        return torch.mean(torch.clamp(best_negative_prediction -
                                      positive_prediction
                                      + 1.0, 0.0))

    def _shuffle(self, sequences, targets):

        shuffle_indices = np.arange(len(targets))
        np.random.shuffle(shuffle_indices)

        return (sequences[shuffle_indices].astype(np.int64),
                targets[shuffle_indices].astype(np.int64))

    def fit(self, sequences, targets, verbose=False):
        """
        Fit the model.

        Arguments
        ---------

        interactions: np.float32 coo_matrix of shape [n_users, n_items]
             the matrix containing
             user-item interactions. The entries can be binary
             (for implicit tasks) or ratings (for regression
             and truncated regression).
        verbose: Bool, optional
             Whether to print epoch loss statistics.
        """

        self._num_items = max(int(sequences.max() + 1),
                              int(targets.max() + 1))

        if self._representation == 'lstm':
            self._net = _gpu(
                LSTMNet(self._num_items,
                        self._embedding_dim,
                        sparse=self._sparse),
                self._use_cuda
            )
        elif self._representation == 'popularity':
            self._net = _gpu(
                PopularityNet(self._num_items,
                              sparse=self._sparse),
                self._use_cuda
            )
        else:
            self._net = _gpu(
                PoolNet(self._num_items,
                        self._embedding_dim,
                        sparse=self._sparse),
                self._use_cuda
            )

        if self._sparse:
            optimizer = optim.Adagrad(self._net.parameters(),
                                      weight_decay=self._l2)
        else:
            optimizer = optim.Adam(self._net.parameters(),
                                   weight_decay=self._l2)

        if self._loss == 'pointwise':
            loss_fnc = self._pointwise_loss
        elif self._loss == 'bpr':
            loss_fnc = self._bpr_loss
        else:
            loss_fnc = self._adaptive_loss

        for epoch_num in range(self._n_iter):

            sequences_tensor = _gpu(torch.from_numpy(sequences),
                                    self._use_cuda)
            targets_tensor = _gpu(torch.from_numpy(targets),
                                  self._use_cuda)

            epoch_loss = 0.0

            for (batch_user,
                 batch_item) in zip(_minibatch(sequences_tensor,
                                               self._batch_size),
                                    _minibatch(targets_tensor,
                                               self._batch_size)):

                user_var = Variable(batch_user)
                item_var = Variable(batch_item)

                optimizer.zero_grad()

                loss = loss_fnc(user_var, item_var, item_var)
                epoch_loss += loss.data[0]

                loss.backward()
                optimizer.step()

            if verbose:
                print('Epoch {}: loss {}'.format(epoch_num, epoch_loss))

    def compute_mrr(self, sequences, targets, num_samples=20):
        """
        Computes the MRR of one-ahead-prediction among
        a sample of possible candidates.

        Will overestimate true MRR but is a lot faster to compute.
        """

        mask = targets > 0

        sequences = Variable(_gpu(torch.from_numpy(sequences.astype(np.int64)),
                                  self._use_cuda), volatile=True)
        targets = Variable(_gpu(torch.from_numpy(targets.astype(np.int64)),
                                self._use_cuda),
                           volatile=True)

        positive_scores = self._net(sequences, targets)

        inversion_counts = positive_scores >= positive_scores

        for _ in range(num_samples):

            negatives = Variable(
                _gpu(
                    torch.from_numpy(np.random.randint(0,
                                                       self._num_items,
                                                       tuple(targets.size()))),
                    self._use_cuda),
                volatile=True
            )

            negative_scores = self._net(sequences, negatives)

            inversion_counts += negative_scores > positive_scores

        return 1.0 / _cpu(inversion_counts.data).numpy().flatten()[mask.flatten()]

    def predict(self, sequences, item_ids):
        """
        Compute the recommendation score for user-item pairs.

        Arguments
        ---------

        item_ids: np.int32 array of shape [n_pairs,]
             an array containing the item ids for the user-item pairs for which
             a prediction is to be computed.
        ratings: bool, optional
             Return predictions on ratings (rather than likelihood of rating)
        """

        sequences = torch.from_numpy(sequences.astype(np.int64))
        targets = torch.from_numpy(item_ids.reshape(-1, 1).astype(np.int64))

        user_var = Variable(_gpu(sequences, self._use_cuda))
        item_var = Variable(_gpu(targets, self._use_cuda))

        out = self._net(user_var, item_var)

        return _cpu(out.data).numpy().flatten()
