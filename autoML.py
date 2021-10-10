from numpy.lib.function_base import append
from sklearn.base import ClassifierMixin, RegressorMixin
from sklearn.model_selection import train_test_split
from sklearn.model_selection import cross_val_score
from sklearn import linear_model
import numpy as np
from itertools import chain, combinations
from sklearn import preprocessing
from sklearn import svm
from sklearn import tree
from sklearn import neighbors
import pandas as pd 
import time
from memory_profiler import memory_usage

class AutoML:
    def __init__(self, ds, y_colname 
                 , algorithms = [linear_model.LinearRegression(), svm.SVR(), tree.DecisionTreeRegressor()
                                 , neighbors.KNeighborsRegressor(), linear_model.LogisticRegression()
                                 , svm.SVC(), neighbors.KNeighborsClassifier(), tree.DecisionTreeClassifier()]
                 , unique_categoric_limit = 15) -> None:
        self.__ds_full = ds
        self.y_colname = y_colname
        self.__ds_onlynums = self.__ds_full.select_dtypes(exclude=['object'])
        self.__X_full = self.__ds_onlynums.drop(columns=[y_colname])
        self.__Y_full = self.__ds_onlynums[[y_colname]]
        self.__results = None
        self.algorithms = algorithms
        self.__unique_categoric_limit = unique_categoric_limit
        self.METRICS_REGRESSION = ['r2', 'neg_mean_absolute_error', 'neg_mean_squared_error']
        self.METRICS_CLASSIFICATION = ['f1', 'accuracy', 'roc_auc']
        #metrics reference: https://scikit-learn.org/stable/modules/model_evaluation.html
        self.MIN_X_Y_CORRELATION_RATE = 0.05 #TODO: define this value dynamically
        
    def addAlgorithm(self, algo):
        self.algorithms.append(algo)
        self.__results = None #cleaning the previous results
    
    def getBestModel(self):
        if self.getBestResult(True) is None:
            return None
        #else
        return self.getBestResult(True).model_instance

    def getBestResult(self, resultWithModel=False):
        if len(self.getResults(resultWithModel)) == 0:
            return None
        #else
        return self.getResults(resultWithModel).iloc[0]
    
    def getResults(self, resultWithModel=False, buffer=True):
        if buffer and self.__results is not None:
            if resultWithModel:                   
                return self.__results
            #else
            return self.__results.drop('model_instance', axis=1)
                   
        #else to get results
        #dataframe format: [algorithm, x_cols,[metrics], model_instance]
        columns_list = ['algorithm', 'features']
        if self.YisCategorical():
            columns_list.extend(self.METRICS_CLASSIFICATION)
        else:
            columns_list.extend(self.METRICS_REGRESSION)
        columns_list.extend(['model_instance', 'train_time', 'mem_max'])
        
        self.__results = pd.DataFrame(columns=columns_list)
        del(columns_list)
        
        y_is_cat = self.YisCategorical()
        y_is_num = not y_is_cat
        
        #features engineering
        features_corr = self.__ds_onlynums.corr()
        #print(features_corr)
        features_candidates = []
        #testing min correlation rate with Y
        for feat_name,corr_value in features_corr[self.y_colname].items():
            if ((abs(corr_value) > self.MIN_X_Y_CORRELATION_RATE)
                and (feat_name != self.y_colname)):
                features_candidates.append(feat_name)
        
        considered_features = []
        features_corr = self.__ds_onlynums[features_candidates].corr()
        #print(features_corr)
        #testing redudance between features
        for i in range(0, len(features_candidates)):
            no_redudance = True
            for j in range(i+1, len(features_candidates)):
                if ((abs(features_corr.iloc[i][j]) > (1-self.MIN_X_Y_CORRELATION_RATE))):
                    no_redudance = False
                    break
            if no_redudance:
                considered_features.append(features_candidates[i])
            
        subsets = all_subsets(considered_features)
        del(considered_features)
        del(features_corr)
        del(features_candidates)
        
        for algo in self.algorithms:
            if  ((y_is_cat and isinstance(algo, RegressorMixin)) #Y is incompatible with algorithm        
                 or (y_is_num and isinstance(algo, ClassifierMixin))#Y is incompatible with algorithm
            ):
                continue
            #else: all right
            print('*** Testing algo ' + str(algo) + '...')
            for col_tuple in subsets:
                if (len(col_tuple) == 0): #empty subsets
                    continue
                #else: all right
                print('cols:' + str(col_tuple) + '...')
                t0 = time.perf_counter()
                mem_max, score_result = memory_usage(proc=(self.__score_dataset, (algo, col_tuple)), max_usage=True, retval=True)
                self.__results.loc[len(self.__results)] = np.concatenate((score_result, [(time.perf_counter() - t0), mem_max]))
        
        self.__results.set_index(['algorithm', 'features'])

        sortby = self.METRICS_REGRESSION[0] #considering the first element the most important
        if y_is_cat:
            sortby = self.METRICS_CLASSIFICATION[0] #considering the first element the most important
            
        self.__results.sort_values(by=sortby, ascending=False, inplace=True)
        
        if resultWithModel:                   
            return self.__results
        #else
        return self.__results.drop('model_instance', axis=1)           
    
    def YisCategorical(self) -> bool:
        y_type = type(self.__Y_full.iloc[0,0])
        
        if (y_type == np.bool_
            or y_type == np.str_):
            return True
        #else
        if ((y_type == np.float_)
            or (len(self.__Y_full[self.y_colname].unique()) > self.__unique_categoric_limit)):
            return False
        #else
        return True    
    
    def YisContinuous(self) -> bool:
        return not self.YisCategorical()
                   
    def __score_dataset(self, algorithm, x_cols):
        X = self.__ds_onlynums[list(x_cols)]
        y = self.__Y_full
        
        #normalizing the variables
        min_max_scaler = preprocessing.MinMaxScaler()
        X_normal = min_max_scaler.fit_transform(X)
        y_normal = min_max_scaler.fit_transform(y)
        
        X_train, X_valid, y_train, y_valid = train_test_split(X_normal, y_normal, train_size=0.8, test_size=0.2, random_state=1102)
        
        model = algorithm

        X_train2 = X_train
        X_valid2 = X_valid
        y_train2 = y_train
        y_valid2 = y_valid
        
        if len(x_cols)==1:
            X_train2 = np.asanyarray(X_train).reshape(-1, 1)
            X_valid2 = np.asanyarray(X_valid).reshape(-1, 1)
            y_train2 = np.asanyarray(y_train).reshape(-1, 1)
            y_valid2 = np.asanyarray(y_valid).reshape(-1, 1)

        model.fit(X_train2, y_train2.ravel())
        
        scoring_list = self.METRICS_REGRESSION
        if self.YisCategorical():
            scoring_list = self.METRICS_CLASSIFICATION
        
        metrics_value_list = []
        
        for scor in scoring_list:
            metrics_value_list.append(np.mean(cross_val_score(model, X_valid2, y_valid2.ravel(), cv=5, scoring=scor)))
        
        result_list =  [str(algorithm).replace('()',''), x_cols]
        result_list.extend(metrics_value_list)
        result_list.append(model)       
        return np.array(result_list, dtype=object)

#util methods
def all_subsets(ss):
    return chain(*map(lambda x: combinations(ss, x), range(0, len(ss)+1)))

#4 Tests
#print(sorted(sklearn.metrics.SCORERS.keys()))

import ds_utils as ut

def testAutoML(ds, y_colname):
    automl = AutoML(ds, y_colname)
    print(automl.getResults().head(10))
    del(automl)
    

if __name__ == '__main__':
    pd.options.display.width = 0
    pd.options.display.max_rows = 0
    
    testAutoML(ut.getDSFuelConsumptionCo2(), 'CO2EMISSIONS')
    testAutoML(ut.getDSPriceHousing_ClassProb(), 'high_price')
    testAutoML(ut.getDSPriceHousing(), 'Price')


