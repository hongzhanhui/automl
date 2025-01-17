import inspect
import logging
import math
import os
import re
import sys
import time
import warnings
from datetime import datetime
from multiprocessing import Pool
from typing import OrderedDict

import numpy as np
import pandas as pd
import pandas as pandas
#import modin.pandas as pd #https://modin.readthedocs.io/
import ray
import scipy.stats as sta
from bitarray import bitarray
from bitarray import util as bautil
from deap import algorithms, base, creator, tools
from imblearn.over_sampling import RandomOverSampler
from joblib import Parallel, delayed, dump
from memory_profiler import memory_usage
from modin.config import ProgressBar
from scipy.special import comb
from sklearn import linear_model, neighbors, preprocessing, svm, tree, utils
from sklearn.base import ClassifierMixin, RegressorMixin
from sklearn.discriminant_analysis import QuadraticDiscriminantAnalysis
from sklearn.ensemble import (AdaBoostClassifier, GradientBoostingClassifier,
                              GradientBoostingRegressor,
                              HistGradientBoostingClassifier,
                              RandomForestClassifier, StackingClassifier,
                              VotingClassifier)
from sklearn.exceptions import ConvergenceWarning
from sklearn.experimental import enable_iterative_imputer
from sklearn.feature_extraction import text
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.gaussian_process import GaussianProcessClassifier
from sklearn.impute import IterativeImputer
from sklearn.metrics import confusion_matrix, get_scorer
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.naive_bayes import GaussianNB, MultinomialNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier
from skopt import BayesSearchCV
from tqdm import tqdm
#from tpot import TPOTClassifier
from xgboost import XGBClassifier, XGBRegressor, XGBRFRegressor

logging.basicConfig(level=logging.INFO, format='%(message)s')

def _flush_intermediate_steps(obj, label_list = [''], dth=None, index=False, output_type='gzip', overwrite=True):
    if dth is None:
        dth = datetime.now()
    #saving df in a csv file
    filename = dth.strftime("%Y%m%d_%H%M%S")
    
    if type(label_list) is str:
        label_list = [label_list]
        
    for label in label_list:
        label = str(label)
        if label != '' and label is not None:
            filename += '_' + label.strip().upper().replace(' ', '_')
    
    filename += '.' + output_type
    
    filedir = './results'
    if not os.path.exists(filedir):
        os.mkdir(filedir)

    file_path = os.path.join(filedir, filename)
    
    if not(overwrite) and os.path.exists(file_path):
        return None

    if output_type == 'gzip':
        obj.to_parquet(file_path, index=index, compression='gzip', engine='fastparquet')
    elif output_type == 'csv':
        obj.to_csv(file_path, index=index)
    elif output_type == 'joblib':
        dump(obj, file_path)
    logging.info(filename + ' saved')
    sys.stdout.flush()

best_results = {} #TODO: relocate to a better place

def flushResults(automl_obj, y):
    global best_results
    
    def processing_test_datasets():
        for file, df_test in automl_obj.test_df_map.items():
            df_predict = pd.DataFrame(columns=automl_obj.y_colname_list)
            for y, result in best_results.items(): #TODO: parallelize
                if automl_obj.predict_proba:
                    df_predict[y] = pd.DataFrame(result['algorithm'].predict_proba(df_test[automl_obj.getFeaturesNames(y)])).iloc[:,1]
                else:
                    y_pred = result['algorithm'].predict(df_test[automl_obj.getFeaturesNames(y)])
                    y_pred = automl_obj.y_encoder_map[y].inverse_transform(np.asanyarray(y_pred).reshape(-1, 1))[:,0]
                    df_predict[y] = pd.DataFrame(y_pred)
            _flush_intermediate_steps(df_predict, ['predict', automl_obj.ds_name, file]
                                    , output_type='csv', overwrite=True)

    #saving the best model
    df = automl_obj.results[y].copy()
    df.reset_index(drop=True, inplace=True)
    best_row = df.iloc[df[automl_obj.main_metric_map[y]].idxmax()]
    main_metric_value = best_row[automl_obj.main_metric_map[y]]
    main_metric_value = int(10000*main_metric_value) #to avoid 0 and generate a unique filename
    #_flush_intermediate_steps(best_model, ['best_model', automl_obj.ds_name, y]
    #                          , output_type='joblib', dth=automl_obj.start_time, overwrite=True)

    if y not in best_results or best_results[y][automl_obj.main_metric_map[y]] < main_metric_value:
        best_results[y] = best_row
        if len(best_results.keys()) == len(automl_obj.y_colname_list):
            processing_test_datasets()
        
        #convert object to string
        df['algorithm'] = df['algorithm'].astype(str)
        df['features'] = df['features'].astype(str)
        df['confusion_matrix'] = df['confusion_matrix'].astype(str)
        _flush_intermediate_steps(df, ['RESULTS', automl_obj.ds_name, y], output_type='csv')
    
def features_corr_level_Y(i, X, y, threshold):
    #features engineering
    #testing correlation between X and Y
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        corr = sta.pearsonr(X, y)[0]
    if ( (corr != corr) #NaN value for correlation because constant feature
        or (abs(corr) < threshold)
        ):
        return None#x[i] below the threshold
    #else: feature ok with Y
    return i

def features_corr_level_X(i, X_0, X_i, threshold):
    #features engineering
    #testing correlation between X_0 and X_i
    for i in range(0, X_i.shape[1]):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            corr = sta.pearsonr(X_0, X_i.iloc[:,i])[0]
        if ( (corr != corr) #NaN value for correlation because constant feature
            or (abs(corr) > threshold)
            ):
            return None#x[i] above the threshold
    #else: feature ok, no redundance
    return i

def float2bigint(float_value):
    if math.isnan(float_value):
        float_value = -1
    return [int(float_value*100000)]

def is_Voting_or_Stacking(a):
    return ((a == VotingClassifier) or (a == StackingClassifier)
            or isinstance(a, VotingClassifier) or isinstance(a, StackingClassifier))
    
def evaluation(individual, automl_obj, y):
    #logging.info(individual)
    algo_instance = individual[-automl_obj.n_bits_algos_map[y]:]
    # in this point the variable algo_instance is a bitarray
    algo_instance = bautil.ba2int(bitarray(algo_instance)) % len(automl_obj.selected_algos_map[y])
    # in this point the variable algo_instance is an integer
    algo_instance =  automl_obj.selected_algos_map[y][algo_instance]
    # in this point the variable algo_instance is a Class

    if is_Voting_or_Stacking(algo_instance):
        algo_instance = algo_instance(estimators=[])
    else:
        algo_instance = algo_instance()
    # in this point the variable algo_instance is a object with the default parameters
    
    col_tuple = individual[:len(automl_obj.X_bitmap_map[y])-automl_obj.n_bits_algos_map[y]]
    col_tuple = tuple([automl_obj.X_train_map[y].columns[i] for i, c in enumerate(col_tuple) if c == 1])
    
    if len(col_tuple)==0:
        return float2bigint(-1)

    #seeking for some previous result
    previous_result = automl_obj.results[y][(automl_obj.results[y]['algorithm'].__class__ == algo_instance.__class__) 
                                        & (automl_obj.results[y]['features'] == col_tuple)]
    if previous_result.shape[0]>0:
        return float2bigint(previous_result.iloc[0][automl_obj.main_metric_map[y]])
    
    if is_Voting_or_Stacking(algo_instance):
        #getting the top 3 best results group by algorithm
        best_estimators = []
        automl_obj.results[y].sort_values(by=automl_obj.main_metric_map[y], ascending=False, inplace=True)
        for row in automl_obj.results[y].iterrows():
            if len(best_estimators)==3:
                break
            
            if is_Voting_or_Stacking(row[1]['algorithm']):
                continue
            
            candidate_algo = row[1]['algorithm']
            candidate_algo.set_params(**row[1]['params'])
            
            if candidate_algo.__class__ not in [x.__class__ for x in best_estimators]:
                best_estimators.append(candidate_algo)
        
        if len(best_estimators)<2:
            return float2bigint(-1)
        #else
        algo_instance.estimators = list(zip(['e'+str(i) for i in range(1,len(best_estimators)+1)],best_estimators))
        
    X_train2 = automl_obj.X_train_map[y][list(col_tuple)]#._to_pandas()
    X_test2 = automl_obj.X_test_map[y][list(col_tuple)]#._to_pandas()
    
    if len(col_tuple)==1:
        X_train2 = np.asanyarray(X_train2).reshape(-1, 1)
        X_test2 = np.asanyarray(X_test2).reshape(-1, 1)

    #tunning parameters
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ConvergenceWarning)
        if automl_obj.grid_search:
            opt = GridSearchCV(estimator=algo_instance
                               , param_grid=automl_obj.algorithms[algo_instance.__class__]
                               , scoring=automl_obj.main_metric_map[y]
                               , cv=automl_obj.n_folds_cv
                               , verbose=0, n_jobs=automl_obj.n_jobs
                               )
        else:
            opt = BayesSearchCV(estimator=algo_instance
                                , search_spaces=automl_obj.algorithms[algo_instance.__class__]
                                , scoring=automl_obj.main_metric_map[y]
                                , n_iter=automl_obj.n_inter_bayessearch, cv=automl_obj.n_folds_cv
                                , verbose=0, n_jobs=automl_obj.n_jobs, random_state=automl_obj.RANDOM_STATE
                                )
        opt.fit(X_train2, automl_obj.y_train_map[y])

    result_row = {'algorithm': opt.best_estimator_
            , 'params': opt.best_params_
            , 'features': col_tuple
            , 'n_features': len(col_tuple)
            , 'train_time': opt.cv_results_['mean_fit_time'][opt.best_index_]
            , 'predict_time': opt.cv_results_['mean_score_time'][opt.best_index_]
            }

    if isinstance(result_row['params'], OrderedDict):
        #changing the type to dict (when using BayesSearchCV)
        result_row['params'] = dict(result_row['params'])

    if automl_obj.YisCategorical(y):
        #confusion matrix
        #labels = automl_obj.y_classes_map[y]
        labels = None #TODO: fix this in multi-class case
        result_row['confusion_matrix'] = confusion_matrix(automl_obj.y_test_map[y], opt.best_estimator_.predict(X_test2), labels=labels)

    if (is_Voting_or_Stacking(algo_instance)
        and len(algo_instance.estimators)>0):
        #incluing the estimators in the row
        result_row['params'].update({'estimators': opt.best_estimator_.estimators})

    for scor_str in automl_obj.getMetrics(y):
        result_row[scor_str] = (get_scorer(scor_str)(opt.best_estimator_, X_test2, automl_obj.y_test_map[y]))

    automl_obj.results[y].loc[len(automl_obj.results[y])] = result_row

    log_msg = '*[' + y + '] Model trained:'
    log_msg += ' {:.5f}'.format(result_row[automl_obj.main_metric_map[y]]) 
    log_msg += ' | ' + str(algo_instance)[:str(algo_instance).find('(')] 
    log_msg += ' | ' + str(len(col_tuple)) + ' features'
    params_str = str(result_row['params'])
    params_str = params_str.replace("'n_jobs': " + str(automl_obj.n_jobs) + ",","").replace("  ", " ").replace("{ ", "{").replace(" }", "}")
    log_msg += ' | ' + params_str

    logging.info(log_msg[:150])#show only the 150 first caracteres
        
    flushResults(automl_obj, y)
    return float2bigint(opt.best_score_) #main metric

def gen_first_people(n_features, n_algos, n_bits_algos):
    first_people = []
    X_bitmap_map = bautil.int2ba(1, n_features)
    X_bitmap_map.setall(1)
    for i in range(n_algos):
        c_bitmap = []
        c_bitmap.extend(list(X_bitmap_map))
        c_bitmap.extend(list(bautil.int2ba(i, n_bits_algos)))
        first_people.append(c_bitmap)
    return first_people

def ga_toolbox(automl_obj, y):
    #genetics algorithm: creating types
    with warnings.catch_warnings(): #TODO: solve RuntimeWarning: A class named 'FitnessMax' has already been created...
        warnings.simplefilter("ignore")
        creator.create("FitnessMax", base.Fitness, weights=(1.0,))
        creator.create("Individual", list, fitness=creator.FitnessMax)
    #multiprocessing
    toolbox = base.Toolbox()
    if not(automl_obj.pool is None):
        #toolbox.register("map", pool.map) #TODO: check if it works
        pass

    #genetics algorithm: initialization
    def initPopulation(pcls, ind_init):
        return pcls(ind_init(c) for c in gen_first_people(automl_obj.X_train_map[y].shape[1]
                                                          , len(automl_obj.selected_algos_map[y])
                                                          , automl_obj.n_bits_algos_map[y]))
    toolbox.register("population", initPopulation, list, creator.Individual)
    
    #genetics algorithm: operators
    toolbox.register("evaluate", evaluation, automl_obj=automl_obj, y=y)
    toolbox.register("mate", tools.cxTwoPoint)
    toolbox.register("mutate", tools.mutFlipBit, indpb=0.1)
    toolbox.register("select", tools.selTournament, tournsize=3)
    return toolbox

# source: https://www.kaggle.com/ratan123/m5-forecasting-lightgbm-with-timeseries-splits

def reduce_mem_usage(df, verbose=True):
    numerics = ['int16', 'int32', 'int64', 'float16', 'float32', 'float64']
    start_mem = df.memory_usage(deep=True).sum() / 1024**2
    logging.info('Original memory usage {:5.2f} Mb'.format(start_mem))
    for col in df.columns:
        col_type = df[col].dtypes
        if col_type in numerics:
            c_min = df[col].min()
            c_max = df[col].max()
            if str(col_type)[:3] == 'int':
                if c_min > np.iinfo(np.int8).min and c_max < np.iinfo(np.int8).max:
                    df[col] = df[col].astype(np.int8)
                elif c_min > np.iinfo(np.int16).min and c_max < np.iinfo(np.int16).max:
                    df[col] = df[col].astype(np.int16)
                elif c_min > np.iinfo(np.int32).min and c_max < np.iinfo(np.int32).max:
                    df[col] = df[col].astype(np.int32)
                elif c_min > np.iinfo(np.int64).min and c_max < np.iinfo(np.int64).max:
                    df[col] = df[col].astype(np.int64)  
            else:
                if c_min > np.finfo(np.float16).min and c_max < np.finfo(np.float16).max:
                    df[col] = df[col].astype(np.float16)
                elif c_min > np.finfo(np.float32).min and c_max < np.finfo(np.float32).max:
                    df[col] = df[col].astype(np.float32)
                else:
                    df[col] = df[col].astype(np.float64)    
    end_mem = df.memory_usage(deep=True).sum() / 1024**2
    if verbose: 
        logging.info('Mem. usage decreased to {:5.2f} Mb ({:.1f}% reduction)'.format(
            end_mem, 100 * (start_mem - end_mem) / start_mem))
    return df

def optimize_pandas():
#souce: https://realpython.com/python-pandas-tricks/
    options = {
        'display': {
            'max_columns': None,
            'max_colwidth': 25,
            'expand_frame_repr': False,  # Don't wrap to multiple pages
            'max_rows': 14,
            'max_seq_items': 50,         # Max length of printed sequence
            'precision': 4,
            'show_dimensions': False
        },
        'mode': {
            'chained_assignment': None   # Controls SettingWithCopyWarning
        }
    }

    for category, option in options.items():
        for op, value in option.items():
            pd.set_option(f'{category}.{op}', value)  # Python 3.6+

def ray_init():
    ray.init(ignore_reinit_error=True, _redis_password="password")

def parallel_process_y(automlobj, y):
    y_encoder = None
    y_full = automlobj.y_full[y]
    y_classes = None
    stratify=None

    if automlobj.YisCategorical(y):
        logging.info('[' + y + '] ML problem type: Classification')
        stratify = y_full
        y_classes = np.sort(automlobj.y_full[y].unique())
        if y_full.dtype == 'object':
            #encoding
            y_encoder = OrdinalEncoder(dtype=int)
            y_full = pd.DataFrame(y_encoder.fit_transform(np.asanyarray(automlobj.y_full[y]).reshape(-1, 1)), columns=[y])
            y_full = reduce_mem_usage(y_full, verbose=False)
    else:
        logging.info('[' + y + '] ML problem type: Regression')

    #splitting dataset
    logging.info('[' + y + ']    Splitting dataset...')
    X_train, X_test, y_train, y_test = train_test_split(automlobj.X, y_full, train_size=0.8, test_size=0.2, random_state=automlobj.RANDOM_STATE, stratify=stratify)
    logging.info('[' + y + ']   X_train dimensions: ' + str(X_train.shape))
    logging.info('[' + y + ']   y_train dimensions: ' + str(y_train.shape))
    y_train = np.asanyarray(y_train).reshape(-1, 1).ravel()
    y_test = np.asanyarray(y_test).reshape(-1, 1).ravel()

    #running feature engineering in parallel
    if automlobj.features_engineering:
        n_cols = X_train.shape[1]
        logging.info('[' + y + '] Features engineering - Testing correlation with Y...')
        considered_features = Parallel(n_jobs=automlobj.n_jobs, backend="threading")(delayed(features_corr_level_Y)
                                (j
                                , X_train.iloc[:,j]#._to_pandas()
                                , y_train
                                , automlobj.min_x_y_correlation_rate)
                                for j in range(0, n_cols))
        considered_features = [x for x in considered_features if x is not None]
        X_train = X_train.iloc[:,considered_features]
        X_test = X_test.iloc[:,considered_features]
        
        def n_features_2str():
            return "{:.2f}".format(100*(1-len(considered_features)/automlobj.X.shape[1])) + "% (" + str(len(considered_features)) + " remained)"
        
        logging.info('[' + y + ']   Features engineering - Features reduction after correlation test with Y: ' + n_features_2str())
        
        if automlobj.do_redundance_test_X:
            logging.info('[' + y + '] Features engineering - Testing redudance between features...')    
            
            n_cols = X_train.shape[1]
            considered_features = Parallel(n_jobs=automlobj.n_jobs, backend="threading")(delayed(features_corr_level_X)
                                    (j
                                    , X_train.iloc[:,j]#._to_pandas()
                                    , X_train.iloc[:,j+1:]#._to_pandas()
                                    , (1-automlobj.min_x_y_correlation_rate))
                                    for j in range(0, n_cols-1))

            considered_features = [x for x in considered_features if x is not None]
            X_train = X_train.iloc[:,considered_features]
            X_test = X_test.iloc[:,considered_features]
            
            logging.info('[' + y + ']   Features engineering - Features reduction after redudance test: ' + n_features_2str())
    
    if automlobj.flush_intermediate_steps:
        col_names = list(X_train.columns)
        trans_df = pandas.DataFrame(columns=col_names)
        _flush_intermediate_steps(trans_df, label_list=[automlobj.ds_name, 'AFTER_FEATENG', y]
                                    , output_type='csv')            
    return (y, list(X_train.columns), y_encoder, y_full
            , y_classes, X_train, X_test, y_train, y_test)

def parallel_process_fit(y, metrics, y_is_cat):
    #dataframe format: ['algorithm', 'params', 'features', 'n_features', 'train_time', 'predict_time', 'mem_max', <metrics>]
    columns_list_base = ['algorithm', 'params', 'features', 'n_features', 'train_time', 'predict_time', 'mem_max']
    columns_list_base.extend(metrics)
    if y_is_cat:
        columns_list_base.append('confusion_matrix')

    map_columns_list = {}
    map_columns_list[y] = columns_list_base
    
    results_df = pandas.DataFrame(columns=columns_list_base)
    
    return (y, map_columns_list, results_df)

def parallel_tfidf(col_name, X_i):
    X_i = preprocess_text(X_i)
    my_stop_words = text.ENGLISH_STOP_WORDS#.union(["book"])
    vectorizer = TfidfVectorizer(ngram_range=(1,4), max_features=750
                                , stop_words=my_stop_words
                                , strip_accents='ascii'
                                , max_df=0.9, min_df=0.01)
    
    X_tfidf = vectorizer.fit_transform(X_i)
    
    X_tfidf = pd.DataFrame(X_tfidf.toarray())
    X_tfidf.columns = vectorizer.get_feature_names_out(X_tfidf.columns)
    X_tfidf = X_tfidf.add_prefix(col_name + '_')
    
    return (col_name, X_tfidf, vectorizer)

def default_algorithms(n_jobs):
    return {
        #classifiers
        #https://scikit-learn.org/stable/auto_examples/classification/plot_classifier_comparison.html
        KNeighborsClassifier: 
            {"n_neighbors": [3,5,7,9,11,13,15,17],
             "p": [2, 3],
             "n_jobs": [n_jobs],
             },
        SVC:
            {"C": [0.001, 0.01, 0.1, 1, 10, 100, 1000],
             "gamma": ["auto", "scale"],
             "class_weight": ["balanced", None],
             "probability": [True]},
        GaussianProcessClassifier:{
            "copy_X_train": [False],
            "warm_start": [True, False],
            "n_jobs": [n_jobs],},
        DecisionTreeClassifier:{
            "criterion": ["gini", "entropy"],
            },
        RandomForestClassifier:{
            "n_estimators": [120,300,500,800,1200],
            "max_depth": [None, 5, 8, 15, 20, 25, 30],
            "min_samples_split": [2, 5, 10, 15, 100],
            "min_samples_leaf": [1, 2, 5, 10],
            "max_features": [None, "sqrt", "log2"],
            "n_jobs": [n_jobs],
            },
        MLPClassifier:{
            "learning_rate": ['constant', 'invscaling', 'adaptive'], 
            'momentum' : [0.1, 0.5, 0.9], 
            },
        AdaBoostClassifier:{
            "algorithm": ["SAMME", "SAMME.R"],
            },
        GaussianNB:{
            "priors": [None],
            },
        QuadraticDiscriminantAnalysis:{
            "priors": [None],
            },
        XGBClassifier:{
            "eta": [0.01, 0.015, 0.025, 0.05, 0.1],
            "gamma": [0.05, 0.1, 0.3, 0.5, 0.7, 0.9, 1],
            "max_depth": [3, 5, 7, 9, 12, 15, 17, 25],
            "min_child_weight": [1, 3, 5, 7],
            "subsample": [0.6, 0.7, 0.8, 0.9, 1],
            "colsample_bytree": [0.6, 0.7, 0.8, 0.9, 1],
            "lambda": [0.01,0.1,1],
            "alpha": [0, 0.1, 0.5, 1],
            "use_label_encoder": [False],
            "eval_metric": ['mlogloss'],
            },
        MultinomialNB:{
            "fit_prior": [True, False],
            }, 
        GradientBoostingClassifier:{
            "loss": ["deviance"],
            },
        HistGradientBoostingClassifier:{
            "warm_start": [True, False],
            },
        #TPOTClassifier(verbosity=0, n_jobs=self.n_jobs):{},
        linear_model.LinearRegression:{
            "fit_intercept": [True, False],
            "n_jobs": [n_jobs],
            },
        linear_model.LogisticRegression:{
            "C": [0.001, 0.01, 0.1, 1, 10,
                  100, 1000],
            "n_jobs": [n_jobs],
            },
        VotingClassifier:{
            "voting": ["soft"],
            "n_jobs": [n_jobs],
            },
        StackingClassifier:{
            "stack_method": ["auto"],
            "n_jobs": [n_jobs],
            },
        #regressors        
        XGBRegressor:{},
        XGBRFRegressor:{},
        svm.SVR:{},
        tree.DecisionTreeRegressor:{},
        neighbors.KNeighborsRegressor:{},
        GradientBoostingRegressor:{},    
    }    
  
def preprocess_text(text_data):
    
    stopwords= ['i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves', 'you', "you're", "you've",\
                "you'll", "you'd", 'your', 'yours', 'yourself', 'yourselves', 'he', 'him', 'his', 'himself', \
                'she', "she's", 'her', 'hers', 'herself', 'it', "it's", 'its', 'itself', 'they', 'them', 'their',\
                'theirs', 'themselves', 'what', 'which', 'who', 'whom', 'this', 'that', "that'll", 'these', 'those', \
                'am', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'having', 'do', 'does', \
                'did', 'doing', 'a', 'an', 'the', 'and', 'but', 'if', 'or', 'because', 'as', 'until', 'while', 'of', \
                'at', 'by', 'for', 'with', 'about', 'against', 'between', 'into', 'through', 'during', 'before', 'after',\
                'above', 'below', 'to', 'from', 'up', 'down', 'in', 'out', 'on', 'off', 'over', 'under', 'again', 'further',\
                'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all', 'any', 'both', 'each', 'few', 'more',\
                'most', 'other', 'some', 'such', 'only', 'own', 'same', 'so', 'than', 'too', 'very', \
                's', 't', 'can', 'will', 'just', 'don', "don't", 'should', "should've", 'now', 'd', 'll', 'm', 'o', 're', \
                've', 'y', 'ain', 'aren', "aren't", 'couldn', "couldn't", 'didn', "didn't", 'doesn', "doesn't", 'hadn',\
                "hadn't", 'hasn', "hasn't", 'haven', "haven't", 'isn', "isn't", 'ma', 'mightn', "mightn't", 'mustn',\
                "mustn't", 'needn', "needn't", 'shan', "shan't", 'shouldn', "shouldn't", 'wasn', "wasn't", 'weren', "weren't", \
                'won', "won't", 'wouldn', "wouldn't"]    
    
    def decontracted(phrase):
        # specific
        phrase = re.sub(r"won't", "will not", phrase)
        phrase = re.sub(r"can\'t", "can not", phrase)
        # general
        phrase = re.sub(r"n\'t", " not", phrase)
        phrase = re.sub(r"\'re", " are", phrase)
        phrase = re.sub(r"\'s", " is", phrase)
        phrase = re.sub(r"\'d", " would", phrase)
        phrase = re.sub(r"\'ll", " will", phrase)
        phrase = re.sub(r"\'t", " not", phrase)
        phrase = re.sub(r"\'ve", " have", phrase)
        phrase = re.sub(r"\'m", " am", phrase)
        return phrase    
    
    preprocessed_text = []
    # tqdm is for printing the status bar
    for sentence in tqdm(text_data):
        sent = decontracted(sentence)
        sent = sent.replace('\\r', ' ')
        sent = sent.replace('\\n', ' ')
        sent = sent.replace('\\"', ' ')
        sent = re.sub('[^A-Za-z0-9]+', ' ', sent)
        # https://gist.github.com/sebleier/554280
        sent = ' '.join(e for e in sent.split() if e.lower() not in stopwords)
        preprocessed_text.append(sent.lower().strip())
    return preprocessed_text  
class AutoML:
    def __init__(self, ds_source, y_colname = 'y'
                 , algorithms = None
                 , unique_categoric_limit = 25 
                 , min_x_y_correlation_rate = 0.01
                 , pool = None
                 , ds_name = None
                 , ngen = 10
                 , metrics = None
                 , features_engineering = True
                 , grid_search = False
                 , n_inter_bayessearch = 30
                 , ds_source_header='infer'
                 , ds_source_header_names=None
                 , flush_intermediate_steps = False
                 , ds_sample_frac = 1
                 , do_redundance_test_X = False
                 , n_jobs = 1
                 , n_folds_cv = 10
                 , drop_nan_values = False
                 , predict_proba = False
                 ) -> None:
        self.start_time = datetime.now()

        #logging.basicConfig(format='%(asctime)s - %(message)s', datefmt='%d-%b-%y %H:%M:%S')
                
        ProgressBar.enable()
        
        #ray_init()
        optimize_pandas()
        
        #initializing variables
        self.results = {}
        self.algorithms = algorithms
        if algorithms is None:
            self.algorithms = default_algorithms(n_jobs)
        self.__unique_categoric_limit = unique_categoric_limit
        self.min_x_y_correlation_rate = min_x_y_correlation_rate #TODO: #1 MIN_X_Y_CORRELATION_RATE: define this value dynamically
        self.RANDOM_STATE = 1102
        self.ds_name = ds_name
        self.ngen = ngen
        self.pool = pool
        self.grid_search = grid_search
        self.n_inter_bayessearch = n_inter_bayessearch
        self.features_engineering = features_engineering
        self.do_redundance_test_X = do_redundance_test_X
        self.flush_intermediate_steps = flush_intermediate_steps
        self.n_jobs = n_jobs
        self.n_folds_cv = n_folds_cv
        self.predict_proba = predict_proba
        
        #initializing control maps
        self.selected_algos_map = {}
        self.n_bits_algos_map = {}
        self.X_bitmap_map = {}
        self.main_metric_map = {}
        
        if type(ds_source) == str:
            if ds_source.endswith('.csv'):
                ds_source = pd.read_csv(ds_source, header=ds_source_header, names=ds_source_header_names)
            elif ds_source.endswith('.gzip'):
                ds_source = pd.read_parquet(ds_source)
        
        logging.info('Optimizing the source dataset:')
        ds = reduce_mem_usage(ds_source)
        del(ds_source) #free memory
        logging.info('Original dataset dimensions: ' + str(ds.shape))
        
        #NaN values
        self.iterative_imputer = None
        if drop_nan_values:
            ds = ds.dropna()
            logging.info('Dataset dimensions after drop NaN values: ' + str(ds.shape))
        
        #shuffle data to minimize bias tendency
        ds = ds.sample(frac=ds_sample_frac)
        
        if flush_intermediate_steps:
            _flush_intermediate_steps(ds, [self.ds_name, 'sample_frac', str(int(ds_sample_frac*100))])

        self.y_colname_list = y_colname
        if type(self.y_colname_list) == str:
            self.y_colname_list = [self.y_colname_list]

        #initializing control maps
        self.y_full = ds[self.y_colname_list]
        self.y_encoder_map = {}
        self.y_classes_map = {}
        self.X_train_map = {}
        self.X_test_map = {}
        self.y_train_map = {}
        self.y_test_map = {}
        self.y_is_categoric_map = {}

        #setting X
        self.X = ds.drop(self.y_colname_list, axis=1)
        del(ds)
        self.__onehot_encoder = OneHotEncoder(sparse=False, dtype=int)
        self.hot_columns = []
        self.str_columns = []
        for i, col in enumerate(self.X.columns):
            if self.X.dtypes[i] == object: 
                if len(self.X[col].unique()) <= self.__unique_categoric_limit:
                    self.hot_columns.append(col)
                else:
                    self.str_columns.append(col)
        
        self.tfidf_vectorizers_map = {}
        
        if len(self.str_columns) > 0:
            #do tfidf
            result_list = Parallel(n_jobs=self.n_jobs, backend='multiprocessing')(delayed(parallel_tfidf) 
                                    (col_name, self.X[col_name])
                                    for col_name in self.str_columns)
            for result in result_list:
                self.tfidf_vectorizers_map[result[0]] = result[2]
                self.X = pd.concat([self.X.reset_index(drop=True), result[1]], axis=1)    
                
            self.X = self.X.drop(self.str_columns, axis=1)
            logging.info('X dimensions after Tfidf: ' + str(self.X.shape))
            
        if len(self.hot_columns) > 0:
            logging.info('One hot encoder columns: ' +str(self.hot_columns))
            self.__onehot_encoder.fit(self.X[self.hot_columns])
            self.hot_cols_names = []
            for i, name in enumerate(self.__onehot_encoder.feature_names_in_):
                for cat in self.__onehot_encoder.categories_[i]:
                    self.hot_cols_names.append(name + '_' + str(cat).lower().replace(' ','_'))
            temp_df = self.__onehot_encoder.transform(self.X[self.hot_columns])
            temp_df = pd.DataFrame(temp_df , columns=self.hot_cols_names) 
            self.X = pd.concat([self.X.reset_index(drop=True), temp_df], axis=1)
            self.X = self.X.drop(self.hot_columns, axis=1)
            del(temp_df)
            logging.info('X dimensions after One hot encoder: ' + str(self.X.shape))

        if not drop_nan_values:
            #inpuiting values for X NaN values
            self.iterative_imputer = IterativeImputer(random_state=self.RANDOM_STATE)
            if sum(self.X.isna().sum()) > 0: #there are nan values
                self.X = pd.DataFrame(self.iterative_imputer.fit_transform(self.X)
                                        , columns=self.X.columns)

        #normalizing the variables
        logging.info('Normalizing the variables...')
        self.scaler = preprocessing.MinMaxScaler()
        self.X = pd.DataFrame(self.scaler.fit_transform(self.X), columns=self.X.columns) 

        logging.info('Optimizing the dataset X after Normalization:')
        self.X = reduce_mem_usage(self.X)
        logging.info('X dimensions after Normalization: ' + str(self.X.shape))

        self.metrics_regression_map = metrics
        self.metrics_classification_map = metrics
        if metrics is None:
            self.metrics_regression_map = {}
            self.metrics_classification_map = {}
            for y in self.y_colname_list:
                self.metrics_regression_map[y] = ['r2', 'neg_mean_absolute_error', 'neg_mean_squared_error']
                self.metrics_classification_map[y] = ['roc_auc', 'f1', 'accuracy']
        #metrics reference: https://scikit-learn.org/stable/modules/model_evaluation.html

        #initializing control maps
        for y in self.y_colname_list:
            self.YisCategorical(y)
        
        result_list = Parallel(n_jobs=self.n_jobs, backend='multiprocessing')(delayed(parallel_process_y) 
                                (self, y)
                                for y in self.y_colname_list)
            
        selected_features = []
        for tuple_result in result_list:
            y = tuple_result[0]
            selected_features.append(tuple_result[1])
            self.y_encoder_map[y] = tuple_result[2]
            self.y_full[y] = tuple_result[3]
            self.y_classes_map[y] = tuple_result[4]
            if self.YisCategorical(y):
                if len(self.y_classes_map[y]) > 2: #multi-class classification
                    #adjusting the metrics
                    for j, m in enumerate(self.metrics_classification_map[y]):
                        if m == 'f1':
                            self.metrics_classification_map[y][j] = 'f1_weighted'
                        #elif m == 'roc_auc':
                        #    self.metrics_classification_map[y][j] = 'roc_auc_ovr_weighted'
                        #elif m == 'accuracy':
                        #    self.metrics_classification_map[y][j] = 'balanced_accuracy'
                        elif m == 'recall':
                            self.metrics_classification_map[y][j] = 'recall_weighted'
                        elif m == 'precision':
                            self.metrics_classification_map[y][j] = 'precision_weighted'
                    #roc_auc is not available for multi-class
                    self.metrics_classification_map[y] = [m for m in self.metrics_classification_map[y] if m != 'roc_auc'] 
                    
            logging.info('[' + y + '] Applied metrics: ' + str(self.metrics_classification_map[y]))
            #X_train, X_test, y_train, y_test
            self.X_train_map[y] = tuple_result[5]
            self.X_test_map[y] = tuple_result[6]
            self.y_train_map[y] = tuple_result[7]
            self.y_test_map[y] = tuple_result[8]
            
        if self.flush_intermediate_steps:
            features_set = set()
            for feat_list in selected_features:
                features_set = features_set.union(set(feat_list))
            
            selected_features = list(features_set)
            
            selfeat_df = pandas.DataFrame(columns=list(selected_features))
            _flush_intermediate_steps(selfeat_df, label_list=[self.ds_name, 'SELECTED_FEATURES']
                                        , output_type='csv')            
            
        #balancing the train datasets
        for y in self.y_colname_list:
            logging.info('[' + y + '] Balancing the dataset...')
            logging.info('[' + y + '] X_train dimensions BEFORE Balancing Process: ' + str(self.X_train_map[y].shape))
            over = RandomOverSampler(random_state=self.RANDOM_STATE)
            self.X_train_map[y], self.y_train_map[y] = over.fit_resample(self.X_train_map[y], self.y_train_map[y])
            logging.info('[' + y + '] X_train dimensions AFTER Balancing Process: ' + str(self.X_train_map[y].shape))

        if flush_intermediate_steps:
            pass
            #TODO: add balanced datasets
            _flush_intermediate_steps(pd.concat([self.X.reset_index(drop=True), self.y_full.reset_index(drop=True)], axis=1)
                                        , [self.ds_name, 'NORMAL_BALANCED'])

        #loading test files
        self.test_df_map = {}
        test_path = os.path.abspath('./to_process') #process directory
        if os.path.exists(test_path):
            for file in os.listdir(test_path):
                if file.endswith('.csv'):
                    logging.info('Processing test file: ' + str(file))
                    file_path = os.path.join(test_path, file)
                    df_test = pd.read_csv(file_path)
                    df_test = df_test.drop(columns=self.y_colname_list)
                    
                    #tfidf
                    for col in df_test.columns:
                        if col in self.str_columns:
                            tfidf_vect = self.tfidf_vectorizers_map[col]
                            X_tfidf = tfidf_vect.transform(preprocess_text(df_test[col]))
                            X_tfidf = pd.DataFrame(X_tfidf.toarray())
                            X_tfidf.columns = tfidf_vect.get_feature_names_out(X_tfidf.columns)
                            X_tfidf = X_tfidf.add_prefix(col + '_')
                            df_test = pd.concat([df_test.reset_index(drop=True), X_tfidf.reset_index(drop=True)], axis=1)
                    
                    #hot encoding
                    X_hot = self.__onehot_encoder.transform(df_test[self.hot_columns])
                    X_hot = pd.DataFrame(X_hot , columns=self.hot_cols_names)        
                    df_test = pd.concat([df_test.reset_index(drop=True), X_hot], axis=1)
                    df_test = df_test.drop(self.hot_columns, axis=1)

                    if drop_nan_values:
                        df_test = df_test.dropna()
                        logging.info('Test dataset dimensions after drop NaN values: ' + str(df_test.shape))
                    elif sum(df_test.isna().sum()) > 0: #there are nan values
                        if self.iterative_imputer.estimator is None:
                            self.iterative_imputer.fit(self.X)
                        df_test = pd.DataFrame(self.iterative_imputer.transform(df_test)
                                               , columns=df_test.columns)
                    #saving in control map
                    self.test_df_map[str(file).replace('.csv', '')] = reduce_mem_usage(df_test)

    def clearResults(self):
        self.results = {} #cleaning the previous results
        
    def getBestModel(self):
        if self.getBestResult() is None:
            return None
        #else
        return self.getBestResult().estimator

    def getBestConfusionMatrix(self):
        return self.getConfusionMatrix(0)
    
    def getConfusionMatrix(self, result_index):
        pass
        '''
        if self.YisContinuous():
            return None
        #else: classification problem
        result = self.__fit().iloc[result_index]
        title = self.__class2str(result.algorithm)
        title += ' (' + str(result.n_features) +' features)'
        title += '\n' + str(result.params)
        title = title.replace("'n_jobs': -1,","").replace("  ", " ").replace("{ ", "{").replace(" }", "}")
        
        categories = self.y_classes_map#['Zero', 'One']
        if self.y_encoder_map is not None:
            categories = self.y_encoder_map.categories_[0]
            
        group_names = [] #['True Neg','False Pos','False Neg','True Pos']
        for c in categories:
            group_names.append('True_' + str(c)) 
            group_names.append('False_' + str(c))
            
        custom_metrics = dict(result.loc[self.getMetrics(y)])
        
        return make_confusion_matrix(result.confusion_matrix
                                     , group_names=group_names
                                     , categories=categories
                                     , cmap='Blues'
                                     , title=title
                                     , custom_metrics=custom_metrics);    
        '''
                
    def getBestResult(self):
        pass
        '''
        if len(self.__fit()) == 0:
            return None
        #else
        return self.__fit().iloc[0]
        '''
    def getResults(self, buffer=True):
        return self.__fit(buffer)
        #results_df = self.__fit(buffer).drop(['confusion_matrix', 'n_features'], axis=1)
        #results_df['algorithm'] = results_df['algorithm'].apply(lambda x: self.__class2str(x))
        #results_df['features'] = results_df['features'].apply(lambda x: str(len(x)) + ': ' + str(x).replace('(','').replace(')',''))
        #return results_df

    def __class2str(self, cls):
        cls_str = str(cls)
        return cls_str[cls_str.rfind('.')+1:cls_str.rfind("'")] 

    def __fit(self, buffer=True):

        def is_in_class_tree(cls1, cls2):
            #vide https://docs.python.org/3/library/inspect.html
            return cls1 in inspect.getmro(cls2)

        t0 = time.perf_counter()
        
        result_list = Parallel(n_jobs=self.n_jobs, backend="multiprocessing")(delayed(parallel_process_fit)
                                        (y, self.getMetrics(y), self.YisCategorical(y))
                                        for y in self.y_colname_list)
        
        columns_list_map = {}
        for tuple_result in result_list:
            y = tuple_result[0]
            y_is_cat = self.YisCategorical(y)
            y_is_num = not y_is_cat
            
            columns_list_map.update(tuple_result[1])
            self.results[y] = tuple_result[2]
            self.selected_algos_map[y] = []
            for algo in self.algorithms.keys():
                if  ((y_is_cat and is_in_class_tree(RegressorMixin, algo)) #Y is incompatible with algorithm        
                    or (y_is_num and is_in_class_tree(ClassifierMixin, algo))#Y is incompatible with algorithm
                ):
                    continue
                #else: all right
                self.selected_algos_map[y].append(algo)
            
            #setup the bitmap to genetic algorithm
            self.n_bits_algos_map[y] = len(bautil.int2ba(len(self.selected_algos_map[y])-1))#TODO: analyze de number of bits to use
            n_cols = self.X_train_map[y].shape[1] + self.n_bits_algos_map[y]
            self.X_bitmap_map[y] = bitarray(n_cols)
            self.X_bitmap_map[y].setall(1)

            #main metric column
            self.main_metric_map[y] = self.getMetrics(y)[0] #considering the first element the most important

            logging.info('[' + y + '] Nº of features: '
                + str(self.X_train_map[y].shape[1])
                + ' | Nº of samples: ' + str(self.X_train_map[y].shape[0])
                + ' | Nº of algorithms: ' + str(len(self.selected_algos_map[y])))

        del(result_list)
                
        def ga_process_fit(y):                    
            toolbox = ga_toolbox(self, y)
            #running the GA algorithm
            algorithms.eaSimple(toolbox.population(), toolbox, cxpb=0.8, mutpb=0.3, ngen=self.ngen, verbose=False)
            #free GA memory
            del(toolbox)
            #preparing the results
            self.results[y].sort_values(by=[self.main_metric_map[y], 'predict_time'], ascending=[False,True], inplace=True)
            self.results[y] = self.results[y].rename_axis('train_order').reset_index()
                
        Parallel(n_jobs=self.n_jobs, backend="threading")(delayed(ga_process_fit)
                                                 (y)
                                                 for y in self.y_colname_list)
        
        logging.info('Fit Time (GA): ' + str(int(time.perf_counter() - t0)) + 's')
        return self.results

    def getMetrics(self, y):
        if self.YisCategorical(y):
            return self.metrics_classification_map[y]
        #else:
        return self.metrics_regression_map[y]
    
    def Ytype(self):
        return type(self.y_full.iloc[0,0])
    
    def YisCategorical(self, col_name) -> bool:
        def is_cat():
            y_type = self.y_full[col_name].dtypes
            if (y_type == np.bool_
                or y_type == np.str_):
                return True
            #else
            if len(self.y_full[col_name].unique()) > self.__unique_categoric_limit:
                return False
            if str(y_type)[:3] == 'int' or str(y_type) == 'object':
                return True 
            return all(self.y_full[col_name].apply(lambda x: x.is_integer()))
        
        if col_name not in self.y_is_categoric_map:
            self.y_is_categoric_map[col_name] = is_cat()
        
        return self.y_is_categoric_map[col_name]
    
    def YisContinuous(self, y) -> bool:
        return not self.YisCategorical(y)
    
    def getFeaturesNames(self, y):
        return self.X_train_map[y].columns    

#utilitary methods

from cf_matrix import make_confusion_matrix


def testAutoMLByCSV(csv_path, y_colname):
    return testAutoML(pd.read_csv(csv_path), y_colname=y_colname)

import ds_utils as util


def testAutoML(ds, y_colname):
    automl = AutoML(ds, y_colname, min_x_y_correlation_rate=0.06)
    #automl.setAlgorithm(svm.SVC())
    if automl.YisCategorical():
        logging.info(str(automl.getBestResult().confusion_matrix))
    
    df4print = automl.__fit()
    logging.info(str(df4print.head()))
    logging.info(str(automl.getBestResult()))
    if automl.getBestResult() is not None:
        logging.info(str(automl.getBestResult()['features']))
    del(automl)

if __name__ == '__main__':
    #pool = Pool(processes=10)
    #automl = AutoML('datasets/iris.csv', 'class'
    #                , ds_source_header_names=['sepal_length', 'sepal_width', 'petal_length', 'petal_width', 'class']
    #automl = AutoML(util.getDSWine_RED(), 'quality'
    #automl = AutoML('datasets/viaturas4Model.csv', 'y'
    #ds_test_multiple_y = pd.read_csv('results/20220109_095911_TRANS_100.csv')
    #ds_test_multiple_y['y2'] = (ds_test_multiple_y['y']-1).abs()
    #ds_test_multiple_y.to_csv('datasets/multilple_y.csv', index=False)
    #logging.info(ds_test_multiple_y)
    #exit()    
    #automl = AutoML('results/20220110_145100_VIATURAS_FASTTEST_SAMPLE_FRAC_100.gzip', ['y', 'y2']
    #automl = AutoML('datasets/multilple_y.csv', ['y', 'y2']
    automl = AutoML('datasets/viaturas.csv', 'com_problema'
                    , flush_intermediate_steps = True
                    , ds_sample_frac = 0.01
                    , min_x_y_correlation_rate=0.005
                    , ngen=10
                    , ds_name='viaturas_fast'
                    , algorithms={KNeighborsClassifier: 
                        {"n_neighbors": [3,5,7]
                         , "p": [2, 3]
                         , "n_jobs": [-1]}
                        }
                    , features_engineering=True
                    , do_redundance_test_X=True
                    , n_inter_bayessearch=3)
    print(automl.getResults())
    print(automl.getBestResult())
    print(automl.getBestConfusionMatrix())
    print('finish')
