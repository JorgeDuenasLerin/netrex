import argparse

import numpy as np

from netrex.netrex import FactorizationModel, generate_sequences
from netrex.evaluation import auc_score, mrr_score
from netrex import rnn_data

from lightfm.datasets import fetch_movielens

from lightfm import LightFM
import lightfm.evaluation


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--sparse', action='store_true')

    args = parser.parse_args()
    cuda = args.gpu
    sparse = args.sparse

    sequence_data = rnn_data.fetch_movielens()
    sequences, targets = generate_sequences(sequence_data['train'])

    def _binarize(dataset):

        dataset = dataset.copy()

        dataset.data = (dataset.data >= 0.0).astype(np.float32)
        dataset = dataset.tocsr()
        dataset.eliminate_zeros()

        return dataset.tocoo()

    movielens = fetch_movielens()
    ratings_train, ratings_test = movielens['train'], movielens['test']
    train, test = _binarize(movielens['train']), _binarize(movielens['test'])

    embedding_dim = 64
    minibatch_size = 4096
    n_iter = 5

    # lfm = LightFM(no_components=embedding_dim, loss='bpr')
    # lfm.fit(train, epochs=5)
    # print(auc_score(lfm, test, train).mean())
    # print(mrr_score(lfm, test, train).mean())

    l2 = 0.0

    model = FactorizationModel(loss='rnn',
                               n_iter=n_iter,
                               l2=l2,
                               embedding_dim=embedding_dim,
                               batch_size=minibatch_size,
                               use_cuda=cuda,
                               sparse=sparse)

    model.fit(test)

    for loss in ('regression', 'truncated_regression'):
        print('Model loss: {}'.format(loss))

        model = FactorizationModel(loss=loss,
                                           n_iter=n_iter,
                                           l2=l2,
                                           embedding_dim=embedding_dim,
                                           batch_size=minibatch_size,
                                           use_cuda=cuda,
                                           sparse=sparse)

        model.fit(ratings_train)

        print(auc_score(model, test, train).mean())
        print(mrr_score(model, test, train).mean())
        print('RMSE:')
        print(np.sqrt(((model.predict(ratings_test.row, ratings_test.col, ratings=True)
                        - ratings_test.data) ** 2).mean()))
        print('RMSE training set:')
        print(np.sqrt(((model.predict(ratings_train.row, ratings_train.col, ratings=True)
                        - ratings_train.data) ** 2).mean()))

        print(model.predict(ratings_test.row, ratings_test.col, ratings=True))

    # embedding_dim *= 2

    for loss in ('pointwise', 'bpr', 'adaptive'):
        print('Model loss: {}'.format(loss))

        model = FactorizationModel(loss=loss,
                                           l2=l2,
                                           n_iter=n_iter,
                                           embedding_dim=embedding_dim,
                                           batch_size=minibatch_size,
                                           use_cuda=cuda,
                                           sparse=sparse)

        model.fit(train)

        print(auc_score(model, test, train).mean())
        print(mrr_score(model, test, train).mean())
