"""Сравнение моделей на фиксированных временных окнах."""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, ExtraTreesClassifier
from sklearn.metrics import average_precision_score, precision_recall_curve
from collector_risk.model import FEATURES, FEATURES_2, add_enhanced_features, make_features


def best_balance(y, probability):
    """Выбирает порог с максимальным балансом precision и recall на валидации."""
    precision, recall, threshold=precision_recall_curve(y,probability)
    i=int(np.argmax(np.minimum(precision[:-1],recall[:-1])))
    return {'precision':float(precision[i]),'recall':float(recall[i]),'threshold':float(threshold[i])}


def main():
    """Сравнивает модели на фиксированном временном разбиении без настройки по тесту."""
    d=add_enhanced_features(make_features(pd.read_csv('artifacts/daily.csv')))
    d=d[d.eligible]
    tr=d[d.date<'2026-05-05']
    va=d[(d.date>='2026-05-05')&(d.date<'2026-06-02')]
    te=d[d.date>='2026-06-02']  # Only the count is reported; no June labels tune a model.
    x=lambda frame,cols:frame[cols].replace([np.inf,-np.inf],np.nan).astype('float32')
    variants={
      'base':(HistGradientBoostingClassifier(max_iter=180,max_leaf_nodes=12,min_samples_leaf=90,
        l2_regularization=5,learning_rate=.05,random_state=42),FEATURES),
      'context':(HistGradientBoostingClassifier(max_iter=180,max_leaf_nodes=12,min_samples_leaf=180,
        l2_regularization=5,learning_rate=.05,random_state=42),FEATURES_2),
      'extra_trees':(ExtraTreesClassifier(n_estimators=200,min_samples_leaf=30,
        max_features=.8,n_jobs=2,random_state=42),FEATURES_2),
    }
    prediction={}
    result={'split':{'train':len(tr),'validation':len(va),'test':len(te)},'validation':{}}
    for name,(model,features) in variants.items():
        model.fit(x(tr,features),tr.target)
        p=model.predict_proba(x(va,features))[:,1]
        prediction[name]=p
        result['validation'][name]={'average_precision':float(average_precision_score(va.target,p)),
                                    **best_balance(va.target,p)}
    # Вес выбран только на валидации; рабочая модель переобучает оба компонента.
    selected=.75*prediction['base']+.25*prediction['context']
    result['validation']['selected_ensemble']={'average_precision':float(average_precision_score(va.target,selected)),
                                                **best_balance(va.target,selected)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    Path('artifacts/model_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
