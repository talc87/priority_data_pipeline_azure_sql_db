import logging
import os
from functools import wraps
import pyodbc
import requests
from flask import Flask, g, jsonify, request
from pymongo import MongoClient
from requests.auth import HTTPBasicAuth
from resources import mongodbHelper

#custom libraries
from resources.priorityDataSource import priorityDataSource
from resources.sqlDwh import sqlDwh


#importing the variables names from the .env file
mongoDbConnStr=os.getenv('mongoDbConnStr')
sqlConnStr = os.getenv('sqlConnStr')
metadataDbName = os.getenv('metadataDbName')
configDbName = os.getenv('configDbName')
configCollectionName = os.getenv('configCollectionName')
datatypeMappingCollectionName = os.getenv('datatypeMappingCollectionName')


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%d-%b-%y %H:%M:%S')
# logging.getLogger('pymongo').setLevel(logging.WARNING)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)


app = Flask(__name__)



#custom decorator to fectch the extractionConfig JSON
def getExtractionconfig(route_function):
    @wraps(route_function)
    def wrapper(*args, **kwargs):
        
        #getting the json body from the request. silent=True --> requestJson will be None of no json was attached
        requestJson = request.get_json(silent=True)

        # Check if the request json is not empty
        if not requestJson or 'datasourceId' not in requestJson.keys():
            return jsonify({'error message': 'The request must include a JSON with a datasourceId key'}), 400
        
        # Use the data in your MongoDB query
        dataSourceId = requestJson['datasourceId']
      
        
        
        g.extractionConfig= mongodbHelper.getExtractionConfig(uri=mongoDbConnStr,dbName=configDbName,collectionName=configCollectionName, dataSourceId=dataSourceId)
        
        # Check if the document was found
        if g.extractionConfig is None:
            return jsonify({'error message': f'No extractionConfig found for data source id: {dataSourceId}'}), 400
        
        logging.debug(f"The extractionConfig for data source id- {g.extractionConfig['_id']} was fetched successfully")
        g.extractionConfig['_id'] = str(g.extractionConfig['_id'])
        
        
        return route_function(*args, **kwargs)

    return wrapper





@app.route('/testExtractionconfigEntities', methods = ['GET'])
@getExtractionconfig # ---> run custom decorator to fetch the getExtractionconfig json
def checkextractionConfigentities():
    extractionConfig = g.extractionConfig
    ptr=priorityDataSource(extractionConfig)
    r = ptr.testExtractionconfigEntities()
    return r




@app.route('/envVariables', methods = ['GET'])
def info():
    return jsonify({'mongoDB connection string': mongoDbConnStr,
                    'SQL connection string' : sqlConnStr,
                    'mongoDB that stores the metadata': metadataDbName,
                    'mongoDB that stores the extraction cofnig documents': configDbName,
                    'mongoDB collection that stores the extraction cofnig documents': configCollectionName,
                    'mongoDB collection that stores the datatypes mapping': datatypeMappingCollectionName
                    
                    }),200


@app.route('/pingApi', methods = ['GET'])
@getExtractionconfig # ---> run custom decorator to fetch the getExtractionconfig json
def pingApi():
    extractionConfig = g.extractionConfig
    logging.debug('ping priority erp, mongoDB and SQL to check that the app can access them')
    
    # ping mongoDB
    logging.debug('ping mongoDB to check the connection is OK')
    client = MongoClient(mongoDbConnStr)
    mongoDbName = client[configDbName]
    mongoDBPing = str(client.mongoDbName.command('ping'))
    logging.debug(mongoDBPing)

    # ping SQL
    logging.debug('ping SQL to check the connection is OK')
    ptr=sqlDwh(extractionConfig,sqlSSL=True)
    sqlPing = ptr.pingDwh()
    logging.debug(sqlPing)
    
    # ping Priority API
    logging.debug('ping Priority API to check the connection is OK')
    api_credentials = HTTPBasicAuth(extractionConfig['apiUsername'],extractionConfig['apiPassword'])
    r = requests.get(extractionConfig['uri'],auth=api_credentials)
    priorityApiResponse = "priority_api_response:" +str(r.status_code)+ " reponse_text: " +str(r.reason)
    logging.debug(priorityApiResponse)

  
    return jsonify({'MomongoDB response': mongoDBPing
                    ,'SQL reponse' : sqlPing
                    ,'Priority api response':priorityApiResponse
                    }),200
                    




@app.route('/extractionConfig', methods = ['GET'])
@getExtractionconfig # ---> run custom decorator to fetch the getExtractionconfig json
def getExtractionConfig():
    logging.debug('collect lastest extractionConfig for datasource')
    extractionConfig = g.extractionConfig
    logging.debug(f'extractionConfig _id = {extractionConfig["_id"]} was fround of the mongoDB')
    return jsonify(extractionConfig),200




@app.route('/extractionConfig', methods = ['POST'])
def postExtractionConfig():
    # getting the json body from the request. silent=True --> requestJson will be None of no json was attached
    requestJson = request.get_json(silent=True)
    logging.debug('inserting extraction config')
    r = mongodbHelper.insertExtractionConfig(mongoDbConnStr,configDbName,configCollectionName,requestJson)
    return r




@app.route('/initialDataLoad', methods=['POST'])
@getExtractionconfig # ---> run custom decorator to fetch the getExtractionconfig json
def initialDataLoad():
    extractionConfig = g.extractionConfig       # --> get the extractionConfig from @getExtractionconfig


    #adding to the mongoDB= MetadataDB collection "datatypeMapping" documents which maps the Priority datatypes to postgres datatypes
    logging.debug(f'running deployMetadataCollection --> deploying to collection {metadataDbName} the documents which map the Priority ERP data types to Postgres DB data types')
    r = mongodbHelper.deployMetadataCollection(mongoDbConnStr,metadataDbName,datatypeMappingCollectionName)
    initialDataLoadResults = {"number of items written to dataMappingCollection": r}
                             
    logging.debug('refreshing the metadata--> extracting the metadata from Priority and insert them to mongoDB collection')
    ptr = priorityDataSource(extractionConfig)
    metadataRefreshResults = ptr.refreshMeatdata()
    

    #buidling the dwh
    logging.debug('setting up the dwh tables')
    ptr=sqlDwh(extractionConfig,sqlSSL=True)
    # Check of the DB exists in, if not create it
    ptr.ifDbExist(True)
    
    # create the tables in the SQL db based on the extractionConfig files.
    sqlDeployedTablesResults = ptr.deployExtractionconfigTables()      # return dict ---> sqlDeployedTables


    #preform a full data loading
    incremental=False
    logging.debug(f'Trigerring "refreshData" method, incrementalFlag set to {incremental}')
    ptr = priorityDataSource(extractionConfig)
    refreshtTablesDataResults = ptr.refreshData(incremental)           # return dict ---> refreshtTablesData
    


    # Merging all the JSON responses into a single dictionary
    r = {
                    "metadataRefreshResults": metadataRefreshResults,
                    "sqlDeployedTables": sqlDeployedTablesResults,
                    "refreshtTablesData": refreshtTablesDataResults,
                    "datatypeMapping": initialDataLoadResults
                    }
    


    return jsonify({request.endpoint: r}), 200




@app.route('/resetDataPlatform',methods=['POST'])
@getExtractionconfig # ---> run custom decorator to fetch the getExtractionconfig json
def resetDataPlatform():
    extractionConfig = g.extractionConfig    
    logging.debug('Deleting all dwh tables in the SQL DWH')
    t=sqlDwh(extractionConfig,sqlSSL=True)
    deleteAllTables = t.deleteTables()

    
    #delete the MongoDB which stores the priority metadata
    logging.debug(f'delete MongoDB {metadataDbName}')
    mongodbHelper.deleteMongoDB(mongoDbConnStr,metadataDbName)

    #adding to the mongoDB= MetadataDB collection "datatypeMapping" documents which maps the Priority datatypes to postgres datatypes
    logging.debug(f'running deployMetadataCollection --> deploying to collection {metadataDbName} the documents which map the Priority ERP data types to Postgres DB data types')
    r = mongodbHelper.deployMetadataCollection(mongoDbConnStr,metadataDbName,datatypeMappingCollectionName)
    initialDataLoadResults = {"number of items written to dataMappingCollection": r}
                             
      
    # refreshing the metadata--> extracting the metadata from Priority and insert them to mongoDB collection
    ptr = priorityDataSource(extractionConfig)
    metadataRefreshResults = ptr.refreshMeatdata()
    

    #buidling the dwh
    logging.debug('setting up the dwh tables')
    ptr=sqlDwh(extractionConfig,sqlSSL=True)
    # create a database in the SQL server if it doesn't exist.
    ptr.createDb()
    
    # create the tables in the SQL db based on the extractionConfig files.
    sqlDeployedTablesResults = ptr.deployExtractionconfigTables()      # return dict ---> sqlDeployedTables


    #preform a full data loading
    incremental=False
    logging.debug(f'Trigerring "refreshData" method, incrementalFlag set to {incremental}')
    ptr = priorityDataSource(extractionConfig)
    refreshtTablesDataResults = ptr.refreshData(incremental)           # return dict ---> refreshtTablesData
    


    # Merging all the JSON responses into a single dictionary
    r = {
                    "deleteAllTables": deleteAllTables,
                    "metadataRefreshResults": metadataRefreshResults,
                    "sqlDeployedTables": sqlDeployedTablesResults,
                    "refreshtTablesData": refreshtTablesDataResults,
                    "initialDataLoad": initialDataLoadResults
                    }


    
    return jsonify({request.endpoint: r}), 200


@app.route('/buildDdwhTables',methods=['POST'])
@getExtractionconfig # ---> run custom decorator to fetch the getExtractionconfig json
def buildDdwhTables():
    extractionConfig = g.extractionConfig 
    ptr = sqlDwh(extractionConfig)
    logging.debug('Deleting all tables in the DWH')
    ptr.deleteTables()

    logging.debug(f"Deploying tables according to the extractionConfig- {extractionConfig['_id']}")
    
    r = ptr.deployExtractionconfigTables()
    return jsonify({request.endpoint: r}), 200






@app.route('/refreshData', methods=['POST'])
@getExtractionconfig # ---> run custom decorator to fetch the getExtractionconfig json
def refreshData():
    extractionConfig = g.extractionConfig       # --> get the extractionConfig from @getExtractionconfig
    
    # fetching the incremental request parameter and convert it to a boolean expression, if not attached set to true
    incrementalParam = request.args.get('incremental', default='true')
    incremental = incrementalParam.lower() in 'true'

    logging.debug(f'Trigerring "refreshData" method, incrementalFlag set to {incremental}')
    ptr = priorityDataSource(extractionConfig)
    r = ptr.refreshData(incremental)
    
    return jsonify({request.endpoint: r}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)  # This is fine since it's running on port 5000 inside the container
