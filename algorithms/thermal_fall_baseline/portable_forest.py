"""Estimator-only JSON export and NumPy inference; does not control alarms."""
import json
from pathlib import Path

import numpy as np


def export_forest(model, path, provenance, radar=False):
    if model.classes_.tolist() != [0, 1]:
        raise ValueError('模型类别必须为非下降0、模拟下降1')
    trees = []
    for estimator in model.estimators_:
        tree = estimator.tree_
        values = tree.value[:,0,:]
        # Works with sklearn versions storing either weighted counts or proportions.
        probability = np.divide(values[:,1],values.sum(1),out=np.zeros(len(values)),where=values.sum(1)>0)
        trees.append(dict(feature=tree.feature.tolist(),threshold=tree.threshold.tolist(),
                          left=tree.children_left.tolist(),right=tree.children_right.tolist(),
                          score1=probability.tolist()))
    data = dict(format='k1_forest_numpy_0.1',n_features=int(model.n_features_in_),
                trees=trees,radar=radar,provenance=provenance)
    Path(path).write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf-8')


class PortableForest:
    def __init__(self, path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('format') != 'k1_forest_numpy_0.1':
            raise ValueError('不支持的模型格式')
        self.n_features = int(data['n_features'])
        self.radar = bool(data['radar'])
        self.provenance = data['provenance']
        self.trees = []
        for item in data['trees']:
            tree = {key:np.asarray(item[key],dtype=np.int64 if key in ('feature','left','right') else np.float64)
                    for key in ('feature','threshold','left','right','score1')}
            count = len(tree['feature'])
            if any(len(value)!=count for value in tree.values()) or not count:
                raise ValueError('树结构不完整')
            branches = np.flatnonzero(tree['feature']>=0)
            if (np.any(tree['feature'][branches]>=self.n_features)
                    or not np.isfinite(tree['threshold']).all()
                    or not np.isfinite(tree['score1']).all()
                    or np.any((tree['score1']<0)|(tree['score1']>1))):
                raise ValueError('树参数无效')
            # Exported trees place descendants after parents; this also prevents cycles.
            for key in ('left','right'):
                if np.any(tree[key][branches]<=branches) or np.any(tree[key][branches]>=count):
                    raise ValueError('树节点越界或包含环')
            self.trees.append(tree)
        if not self.trees:
            raise ValueError('模型缺少树')

    def predict_score(self, features):
        x = np.asarray(features,dtype=np.float32)
        if x.ndim!=2 or x.shape[1]!=self.n_features or not np.isfinite(x).all():
            raise ValueError('特征维度错误或存在无效值')
        result = np.zeros(len(x),dtype=np.float64)
        for tree in self.trees:
            nodes = np.zeros(len(x),dtype=np.int64)
            while True:
                active = np.flatnonzero(tree['feature'][nodes]>=0)
                if not len(active):
                    break
                current = nodes[active]
                left = x[active,tree['feature'][current]]<=tree['threshold'][current]
                nodes[active] = np.where(left,tree['left'][current],tree['right'][current])
            result += tree['score1'][nodes]
        return result/len(self.trees)
