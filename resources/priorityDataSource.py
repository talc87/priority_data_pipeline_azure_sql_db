import base64
import logging
import os
import ssl
import uuid
from datetime import datetime, timezone

import pandas as pd
import pymongo
import pyodbc
import pytz
import requests
import xmltodict
from bson import ObjectId
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%d-%b-%y %H:%M:%S')
# logging.getLogger('pymongo').setLevel(logging.WARNING)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)


class priorityDataSource:

    mongoDbConnStr = os.getenv('mongoDbConnStr')
    metadataMongoDB = os.getenv('metadataDbName')
    configMongoDB = os.getenv('configDbName')
    configCollection = os.getenv('configCollectionName')
    datatypeMappingCollection = os.getenv('datatypeMappingCollectionName')




    client = pymongo.MongoClient(mongoDbConnStr
                                ,socketTimeoutMS=None
                                ,tlsAllowInvalidCertificates=True
                                )




    metadataMongoDB = client[metadataMongoDB]
    configMongoDB = client[configMongoDB]
    configCollection = configMongoDB[configCollection]
    datatypeMappingCollection = metadataMongoDB[datatypeMappingCollection]

    sourceSystem='Priority'
    

    def __init__(self,extractionConfig):

        self.datasourceID = extractionConfig['_id'] # --> represtns the data source id
        self.accountID = extractionConfig['accountID'] # --> represtns the account id an account can have several data sources
        
        self.uri = extractionConfig['uri']  # --> the Priority API service uri
        self.entities = extractionConfig['entities']    # --> lsit of dict {entityID: <entityID>, expand: <expand>}
                
        
        #priority user credential
        self.username =extractionConfig["apiUsername"]
        self.password = extractionConfig["apiPassword"]
        
        self.extractionID=str(uuid.uuid4()) # --> the extraction ID is a unique identifier for each extraction, generated each time the extraction is run
        self.extractionTimestampUTC = datetime.now(timezone.utc)
        
        self.priorityTimeZone = pytz.timezone(extractionConfig['systemTimezone'])
        
        #mongoDB instance variable
        self.metadataCollectionMongo = priorityDataSource.metadataMongoDB[self.datasourceID] # --> the metadata collection in the MongoDB has the datasource ID as the collection name
        
        
        #SQL SQL Alchemy engine
        self.sqlDb = 'acc-'+self.accountID
        self.sqlConnStr = os.getenv('sqlConnStr') + self.sqlDb + '?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no'
                
        logging.debug('priorityDataSource __init__ finished successfully')



  
    
    def refreshMeatdata(self)->dict:
        '''
        Description: The method preforms 5 steps:
                        - (1) Delete all the metadata documents in the MongoDB
                        - (2) Extracts the full metadata from the Priority API service in xml format and parse it to JSON format.
                        - (3) Write the JSON into a MongoDB DB.
                        - (4) Convert the Priority datatypes to the CDM (common data model data types)
                        - (5) add the CDM datatypes to the metadata collection in the MongoDB.


        Parameters: None

        Returns: {
                    'acknowledged:': boolean, writing the data completed succesfully
                    ,'inserted_count': int, the number of records added to the DB
                    ,'modifiedCount: int, the number of records which the CDM datatypes was added (expect to be modifiedCount = inserted_count)

                }
        '''

        
        logging.debug('starting refreshMeatdata- extracting datasource metadata nad write them into mongoDB')
        startTimestamp = datetime.now(timezone.utc)
        
        

        #count the documents in the metadata db before the refresh
        countBeforeRefresh = self.metadataCollectionMongo.count_documents({})

        # Delete all the metadata documents in the MongoDB and return the number of documents deleted
        deletedDocuments = self.metadataCollectionMongo.delete_many({}).deleted_count
        

        # Extracts the full metadata from the Priority API service in xml format and parse it to JSON format.
        grossMetadata = self.getMetadata()
        
        
        # Write the JSON into a MongoDB DB.
        updatedmetadata = self.metadataCollectionMongo.insert_many(grossMetadata).acknowledged
        
        # Convert the Priority datatypes to the CDM (common data model data types)
        metadataModifiedDocuemnts = self.__adjustMetadataDatatype()

        endTimestamp = datetime.now(timezone.utc)
  
        fullMetadataExtractLog = {
                                    "metadataRecordsBeforeDelete": countBeforeRefresh
                                    ,"documentsDeleted": deletedDocuments
                                    ,"metadataRecordsExtractedFromApi": len(grossMetadata)
                                    ,"metadataInserted":updatedmetadata
                                    ,"metadataRecordsDatatypedModified": metadataModifiedDocuemnts
                                    ,"startTimestamp": startTimestamp
                                    ,"endTimestamp": endTimestamp
                                    ,"totalTimeSeconds": (endTimestamp - startTimestamp).total_seconds()
                                }   
            
            
            
            
        
        return fullMetadataExtractLog



    def testExtractionconfigEntities(self):
        #itterate over all the entityes in the extractionConfig
        resultList = []
        for i in self.entities:
            logging.info(f'testing the extracting entity {i["EntityID"]} from Priority api')
            entityResponse = self.entityGetRequest(i,incrementalFlag=False,check=True)
            resultList.append(entityResponse)
        
        return resultList





    def refreshData(self,incremental:bool)->dict:
        '''
        description: The method extracts all entities from the Priority API based on the extraction configuration provided on the object creation.
        parameters: None
        returns: None
        
        '''
        stgDatawritten=[]
        
        #itterate over all the entityes in the extractionConfig
        for i in self.entities:
            
            logging.info(f'extracting entity {i["EntityID"]} from Priority api')
            entityResponse = self.entityGetRequest(i,incremental)
            
            
            #check if the api requests returned code 200, if yes --> cget the sjson data
            if entityResponse.ok:
                    
                #convert the reponse to a python dict
                entityJson = entityResponse.json()['value']
                logging.info(f'{len(entityJson)} records were recevied for entity {i["EntityID"]}')
            else:
                logging.error(f'error with api request to entity {i["EntityID"]} \n {entityResponse.text}')
                logging.error(entityResponse.url)
                errMsg = entityResponse.json()['error']['message']
                
                stgDataLog = {
                                        'entityName': i['EntityID'],
                                        'recordsWritten': errMsg
                                        }
                
                stgDatawritten.append(stgDataLog)
                continue
            
            
            #parsing the data entity json to multiple pandas df. getting a list of dict 
            # {
            #   entityName: <entityName>
            #   ,df: <pandas df>
                
            # }
            stgData = self.parsingDf(entityJson,i['EntityID'])


            #itterate over parsed data (entities and sub-entities which were extracted by from the API)
            for d in stgData:
                
                
                #if incremental flag set to 'False' and the table exsists in the dwh --> drop the table
                
                # writing data to stg layer
                #   - table name: {d["entityName"]}'
                #   - dbName- according to the instance variable {datasource name & id}
                #   - dropTableBefore- if the requests was sent with incremental flag = FALSE (full refresh), Drop the table before inserting new values (see pandas to_sql 'if_exists')
                

                stgRecords = self.writeDataToStg(df=d['df'],dbName=self.sqlDb,tableName=d['tableName'], incrementalFlag=incremental)
                stgDataLog = {
                                        'entityName': d['entityName'],
                                        'tableName': 'stg_'+d['tableName'],
                                        'recordsWritten': stgRecords
                                        }
                
                stgDatawritten.append(stgDataLog)
                
            # update the entity lastRun timestamp
            self.updateLastRun(i['EntityID'])


            
        
        return stgDatawritten
    
    
     








    @property
    def authHeader(self)->dict:
        '''
        description: The method returns the authentication header for the Priority API service.
        parameters: None
        returns: dictionary
        
        '''
        credentials = f"{self.username}:{self.password}"
        credentials_b64 = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {credentials_b64}"}
                        




    def getEntitiesList(self)->list:
        '''
        description: The method returns the list of entities which the data source contains.
        parameters: None
        returns: list of the entity names according to the data source metadata.
        for example: [
                        {'_id': entityID}
                        ,{'_id': entityID}
                        
                    ]


        '''
        collection = self.metadataCollectionMongo
        
        # Query to find the entity metadata document
        projection = {"_id": 1}
        
        
        result = collection.find({}, projection)
        
        return result
    
    
    
    
    def writeDatatoMongoDB(self,data:dict,dbName:str,collectionName:str)->dict:
        '''
        description: The methods write the data dictionary to MongoDB.

        parameters: 
                    - data: data to be written into the DB.
                    - dbName: MongoDB DB name which the data will be written
                    - collectionName: collection which the data will be written.
                    
        
        returns:  dictionary
                            {
                            'collection': string, the collection which the was added
                            'db': string, the db which the was added
                            'acknowledged:': boolean, writing the data completed successfully
                            ,'inserted_count': int, the number of records added to the DB.

                        }
        
        '''
        db = priorityDataSource.client[dbName]
        collectionName = db[collectionName]
        result = collectionName.insert_many(data)
        
        # return the db name, the lenghts of document which was written
        return {
                'collection': collectionName
                ,'db' :dbName
                ,'acknowledged:': result.acknowledged
                ,'inserted_count': len(result.inserted_ids)

                }
    
    
    def findMongoDB(self,collectionName:str,dbName:str)->pymongo.cursor.Cursor:
        '''
        description: The methods query MongoDB and retrieve all the documents in the collection.

        parameters: 
                    - dbName: DB name which the data will be queried from
                    - collectionName: the collection which the data will be written.
                    
        
        returns:  pymongo.cursor.Cursor

        '''
                
        db = priorityDataSource.client[dbName]
        collectionName = db[collectionName]
        result = collectionName.find({})
        
        # return the db name, the lenghts of document which was written
        return result
    
    
    
  


    def getMetadata(self)->list:
        '''
        Description: The method extracts the full metadata from the Priority API service in xml format and parse it to JSON format and write the JSON into a MongoDB.

        Parameters: None

        Returns: 
                    list of all the Priority metadata
                    
                
        '''

        #building the metadat api uri
        uri = self.uri+'$metadata'
        headers = self.authHeader
        try:
            response = requests.request("GET", uri, headers=headers)
        except requests.exceptions.RequestException as e:
            logging.error('Error:', e)

        
        #save the metadata xml to a variable
        data_dict = xmltodict.parse(response.text)['edmx:Edmx']['edmx:DataServices']['Schema']['EntityType']
        

        entities_ls = []
        
        #parsing the meatdata xml to a python dict list. each item on the list represents an entity.
        for index, i in enumerate(data_dict, start=0):

            entity = {}
            fields_ls = []
            keys_ls = []
            logging.debug(f"Parsing entities names and description of {i['@Name']}")
            entity['sourceSystem'] = priorityDataSource.sourceSystem

            try:
                entity['desc'] = i['Annotation']['@String']
            except KeyError as error:
                logging.warning(f'Failed to fetch entity description {i["@Name"]}: {error}')

            logging.debug('Fetching entity keys')
            
            try:
                if type(i['Key']['PropertyRef']) == list:
                    for x in i['Key']['PropertyRef']:
                        keys_ls.append (x['@Name'])
                    logging.debug(f"The entity has multiple keys- {keys_ls}")	
                elif type(i['Key']['PropertyRef']) == dict:
                    keys_ls.append (i['Key']['PropertyRef']['@Name'])
                    logging.debug(f"The entity has a single key - {i['Key']['PropertyRef']['@Name']}")
            except KeyError:
                logging.debug(f"The entity {i['@Name']} doesn't has key attribute")
            logging.debug(f"Fetching fields data of entity {i['@Name']}")

            # Case I: The entity's fields are a list object--->loop on all entities
            try:
                for j in i['Property']:
                    field = {}
                    field['fieldName'] = j['@Name']
                    field['SourceDataType'] = j['@Type']

                    try:
                        field['desc'] = j['Annotation']['@String']
                    except (TypeError, KeyError) as error:
                        logging.debug(f"Failed to fetch field description on entity {i['@Name']} on field {j['@Name']}. Error message:{error} ")
                    if field['fieldName'] in keys_ls:
                        field['KeyFlag'] = True 
                    else:
                        field['KeyFlag'] = False
                    fields_ls.append(field)

            # Case II: The entity contains only 1 field---> fetch the data without any loop
            except TypeError as e:
                logging.debug('#Case II: The entity contains only 1 field---> fetch the data without any loop')
                logging.debug(f"The entity {i['@Name']} has a single field. Error message:{e}")
                field = {}
                field['fieldName'] = i['Property']['@Name']
                field['SourceDataType'] = i['Property']['@Type']

                try:
                    field['desc'] = i['Annotation']['@String']
                except (TypeError, KeyError) as error:
                    logging.debug(f"Failed to fetch field description on entity {i['@Name']} on field {i['Property']['@Name']}. Error message:{error} ")
                fields_ls.append(field)

            finally:
                entity['Fields'] = fields_ls
                entity['_id'] =  i['@Name']
                entity['LastModified'] = datetime.utcnow().strftime("%m/%d/%Y, %H:%M:%S")

                try:

                    logging.debug(entity)
                    entities_ls.append(entity)
                except pymongo.errors.DuplicateKeyError as e:
                    logging.warning(f"Caught DuplicateKeyError: {str(e)}")
                    logging.warning(f'{entity["_id"]} already exists in the mongoDB collection. The entity document will be updated')
                          
        
        #The loop iterate over all entities in the entities_ls list and add a key (list) with all primary keys fields in the entity.
        # If the key KeyFlag is set to True the "fieldName" value will be added to the list, otherwise
        # If the key KeyFlag is set to False or there is not key named "KeyFlag" the loop will continue to the next field.
        # finally, the list of the entity primary keys will be added as a field to the entity dictionary
          
        for e in entities_ls:
            EntityPk = [f['fieldName'] for f in e['Fields'] if f.get('KeyFlag', False)]
            
            e['EntityPk'] = EntityPk

        
        return entities_ls
        



    def getEntitySubforms(self,entityId: str, suffix:bool)->list:
        
        '''
        description: The method queries the metadata collection in the MongoDB and returns the subforms of the entity with the given entityId.
        parameters: entityId - the entity id to query the subforms for
                    suffix - a boolean flag to indicate if the entity id is a suffix of the entity id in the metadata collection
        returns: list of subforms of the entity with the given entityId
            example:
                    input:
                            {
                            "entities": [
                                {
                                    "EntityID": "AINVOICES",
                                    "$filter": "IVDATE%20ge%202018-02-23T09:59:00%2B02:00",
                                    "LastRun": "2021-12-14T11:30:11.9797373+02:00",
                                    "expand": [
                                        "AINVOICEITEMS",
                                        "IVORD"
                                        ]
                                    }
                                ]
                            }
                    output when suffix = FALSE: ["AINVOICEITEMS", "IVORD"]
                    output when suffix = TRUE: ["AINVOICEITEMS_SUBFORM", "IVORD_SUBFORM"]


        '''
        logging.info(f'Quering DB: {priorityDataSource.configMongoDB.name} collection: {priorityDataSource.configCollection.name} --> getting subforms list for datasource= {self.datasourceID} entityId= {entityId}, suffix set to {suffix}')
        
        # Query to find the entity metadata document
        query = {"_id": ObjectId(self.datasourceID), "entities.EntityID": entityId}
        projection = {"_id": 0, "entities.$": 1}
        result = priorityDataSource.configCollection.find_one(query,projection)
        
        # if the return document is not blank
        if result:
            
            # Extract the values from the "expand" key
            subforms = result.get("entities", [{}])[0].get("expand", [])     # convert the result to--> ['AINVOICEITEMS', 'IVORD']
            

            
            # if suffix is True add the suffix "SUBFORM", else don't add the suffix
            if suffix:
                subforms = [sub + "_SUBFORM" for sub in subforms]
            
            
            logging.debug(f'returnning the subforms list {subforms}')
            return subforms

            
        else: #the document is empty, the entity doesn'n have subforms.
            subforms=[]
        
        
        logging.debug(f'returnning a blank list of subforms {subforms}')
        return subforms
        
        
    
    def __adjustMetadataDatatype(self):
        count = 0   
        '''
        description: the method adjusts the metadata datatype to the SQL datatype based on the datatype mapping document saved in the MongoDB.
        parameters: None
        returns: None
        
        '''
        
        logging.debug(f"retrieving metadata json for datasourceID= {self.datasourceID}")
        priorityMetadata = list(self.metadataCollectionMongo.find({}))

        
        logging.debug(f"retrieving mapping json")
        # Define the query
        query = {"_id.sourceSystem": self.sourceSystem}
        mapping_doc = list(priorityDataSource.datatypeMappingCollection.find(query))
            
        newMetadata = []
        for doc in priorityMetadata:
        # iterate through each field in the Fields list of the document
            logging.debug(f"updating entity {doc['_id']}")
            
            for field in doc['Fields']:
                # find the corresponding Postgres DB data-type value in list2 based on SourceDataType and sourceSystem
                matching_datatype = next(
                                        (item['AzureSQLDatatype'] for item in mapping_doc if item['_id']['SourceDataType'] == field['SourceDataType'] and item['_id']['sourceSystem'] == doc['sourceSystem']), None
                                        )
                # add the targetDataType field to the field dictionary
                field['targetDataType'] = matching_datatype
                
                
            newMetadata.append(doc)


        # delete all documents in the metadata collection
        self.metadataCollectionMongo.delete_many({})
        result = self.metadataCollectionMongo.insert_many(newMetadata)
        
        
        return len(result.inserted_ids)




 

    
    def getEntityPKFromMongoDB(self,entityId)->list:
        '''
        description: The method retrieves the entity primary key field(s) from the metadata collection in the MongoDB.
        parameters:
                    entityId (str) - the entity ID for which the primary key field(s) will be retrieved
        returns: list of primary key field(s) for the specified entity
        '''
        
        collection = self.metadataCollectionMongo
        
        # Query to find the entity metadata document
        projection = {"_id": 0, "EntityPk": 1}
        query = {"_id": entityId}

        # return a list with all entity pk
        
        result = collection.find_one(query, projection)
        logging.debug(f'returning pk field(s) for company #{self.accountID} for entity- {entityId} - {result}')
        
        return list(result.values())[0]
    
        
            
    def updateLastRun(self,entityID):
        '''
        description: The method updates the 'LastRun' field in the extraction configuration collection in the MongoDB.
        
        parameters: None
        
        returns: None
        
        '''
        

        newLastRun=datetime.now(self.priorityTimeZone).strftime("%Y-%m-%d %H:%M:%S")
        logging.debug(f'updating toe lastRun of entity {entityID} to {newLastRun} @timezone- {self.priorityTimeZone}')


        # The query and update expressions
        query = {"_id": self.datasourceID}
        update = {
            "$set": {
                "entities.$[elem].lastRun": newLastRun
            }
        }
        array_filters = [{"elem.EntityID": entityID}]
        

        '''
        query = {
                    "_id": self.datasourceID,
                    "entities.entityId": entityID

                }
        
        set= { "$set": { "entities.$.lastRun": newLastRun } }

        '''
        # r = priorityDataSource.configCollection.update_one(query,set)

        r = priorityDataSource.configCollection.update_one(query, update, array_filters=array_filters)

        logging.debug(f'lastRun @entity {entityID} updated to {newLastRun}')
        
        # return newLastRun
        

    
    def entityGetRequest(self,entity:dict, incrementalFlag:bool = True, check:bool = False):
        '''
        Makes a GET request to the specified API URL using the provided API credentials.
   
        parameters: 
                    - entity (dict) - the entity to extract
                    - incrementalFlag (bool) - a flag to indicate if the extraction is incremental or full refresh
        
        returns: requests.models.Response object
        '''
        
        #initialize the request parameters
        requestParams = {}
        
        
        #check if the filter flag is set to True
        
        if entity.get('filterFlag',False):   #check #filterFlag = True if filterFlag key is missong evaluate to False
            #filterFlag set to True --> the request will be sent to fetch the data since a specific point of time    
            fieldParam = entity['filterField']

            if incrementalFlag:
                # The daata will be fetched from the last run
                startDate=entity['lastRun']
                logging.debug(f'filter flag set to {entity["filterFlag"]}, incrementalFlag set to {incrementalFlag} --> the entity should be filtered  according to field {fieldParam} > {startDate}')

            else:
                # The daata will be fetched from the startDate, from the original benchmark period set by the user
                #creating the filter by field and date parameter
                startDate = entity['dataStartDate']
                logging.debug(f'filter flag set to {entity["filterFlag"]}, incrementalFlag set to {incrementalFlag} --> the entity should be filtered  according to field {fieldParam} > {startDate}')
            
            
            
            #The extraction will use the startDate and fieldParam ---> constructing the Odata filter format
            logging.debug('formating the startDate timestamp & the fieldParam')
            
            # Parse and format the timestamp
            dt = datetime.strptime(startDate, '%Y-%m-%d %H:%M:%S')
            dt = self.priorityTimeZone.localize(dt)
            startDate = dt.isoformat()
            # startDate = quote_plus(dt)


            # Construct the OData filter query
            # requestParams = f"{fieldParam} ge {startDate}"
            requestParams['$filter']=f"{fieldParam} ge {startDate}"
        
        else:
            # filterFlag set to False the entire table will be refresh not matter what
            # the startDate and the fieldParam to None
            startDate = None
            fieldParam=None
            logging.debug(f'No filter flag is set to {entity["EntityID"]}, the entire table will be truncated and re-writes')
        

       
        #check if the user asked to extract subforms with this main entity --> if extractionConfig includes $expand key and the $expand list is not empty
        expandFlag = 'expand' in entity and entity['expand']!=[]
        if expandFlag:
            logging.debug(f"the entity include subforms- {entity['expand']}")
            subformsParam = [x + "_SUBFORM" for x in entity['expand']]
            subformsParam = ','.join(subformsParam)

            requestParams['$expand'] = subformsParam
        else:
            logging.debug('the entity does not include subforms')
            subformsParam = None


        if check:
            requestParams["$top"] = 1
            r = requests.get(   
                    self.uri+entity['EntityID']
                    ,headers= self.authHeader 
                    ,params = requestParams
                    )
            logging.debug(f'for url: {r.url} the reponse is {r.status_code}')
            return {
                    "entity": entity['EntityID']
                    ,'url': r.url
                    ,"result": r.status_code
                    }


        
        try:
        
            r = requests.get(   
                                self.uri+entity['EntityID']
                                ,headers= self.authHeader 
                                ,params = requestParams
                                )
            
            logging.debug(f'request to entity {entity["EntityID"]} sent successfully\n{r.url}')

            if r.status_code == 200:
            # Process the response data
                logging.debug(f'request to entity {entity["EntityID"]} sent successfully and return satus code ={r.status_code}')
                logging.debug(f'The url is {r.url}')
            if r.status_code >200:
                logging.error(f'request to entity {entity["EntityID"]} returned satus code ={r.status_code} \n {r.text}')
                
            
        except requests.exceptions.RequestException as e:
            logging.debug(f'request to entity {entity["EntityID"]} sent UN-successfully')
        
        return r
    


    def parsingDf(self,data:dict,entityId: str)->list:
        '''
        description: The method parses the entity data JSON to multiple pandas dataframes and splits the data to the main entity and subforms, each of them will be parsed to a pandas df.
        
        parameters:
                    data (dict) - the entity data JSON
                    entityId (str) - the entity ID 
        
        returns: list of dict {entityName: <entityName>, df: <pandas df>}
        
        '''
        dfList=[]
        
        #retrieving the entity pk and addting 2 fields in order to log the extraction
        pk = self.getEntityPKFromMongoDB(entityId)
        
        #converting the data dict to pandas df
        df = pd.DataFrame(data)

        #adding extractionID & utc timestamp to the df
        df['extractionID']=self.extractionID
        df['extractionTimestampUTC'] = self.extractionTimestampUTC
        



        #get the entity subforms based on the extraction config
        entitySubforms = self.getEntitySubforms(entityId,False)
        entitySubformsSuffix = self.getEntitySubforms(entityId,True)

                
        entityColumns = set(df.columns)
        subformsColumns = set(entitySubformsSuffix)
        mainEntityColumns = list(entityColumns-subformsColumns)
        
        #create a df of the main entity df with all the relevant columns
        mainEntityDf = df[mainEntityColumns]


        #change columns name to lower case
        mainEntityDf.columns=map(str.lower,mainEntityDf.columns)
        

        #generating the first dict on the list and adding it to the final list.
        dict= {
                'tableName': entityId.lower(),
                'entityName': entityId.lower(),
                'df': mainEntityDf}
        dfList.append(dict)
        
        
        #itterating over the entity subforms and adding them to the final list
        for i in entitySubforms:
            
            subformDF = pd.json_normalize(data,record_path=i + "_SUBFORM",meta=pk)      #--> parsing the reponse df to a list of df. need to add "_SUBFORM" suffix in order to parsing work
            if not subformDF.empty:
                #adding extractionID & utc timestamp to the subform df
                subformDF['extractionTimestampUTC'] = self.extractionTimestampUTC
                subformDF['extractionID']=self.extractionID

                #change columns name to lower case
                subformDF.columns=map(str.lower,subformDF.columns)
                
                dict= {
                            'tableName': i.lower()
                            ,'entityName': i.lower() + "_subform"
                            ,'df': subformDF}
                
                
                
                dfList.append(dict)

        
        
            

        return dfList
    
    

    def getEntityMetadata(self,entityId):
        '''
        description: The method queries the metadata collection in the MongoDB for the specified entity metadata.

        parameters:
                    entityId (str) - the entity ID
        
        returns: dict of the entity metadata
        
        '''
        logging.debug(f'querying the metadata for entity {entityId}')
        collection = self.metadataCollectionMongo
        
        # Query to find the entity metadata document
        query = {"_id": entityId}
        projection = {"_id": 0, "Fields": 1}

        
        result = collection.find_one(query,projection)
        # r = [entity.get('EntityID') for entity in result.get('entities', [])]
               
        logging.debug(f'returnning metadata for entity {entityId}')

        
        return result
    

    def writeDataToStg(self,df:pd.DataFrame, dbName: str, tableName:str,incrementalFlag:bool)->int:
        '''
        description: The method writes the data to the staging table in the SQL database. the stg doesn't have any indexes and is used for data validation and transformation.

        parameters:
                    df (pd.DataFrame) - the data to write
                    tableName (str) - the table name to write the data to (without the stg_ prefix and should be the entity ID)
        
        returns: int - the number of records written to the table.
        
        
        '''
        
        # if dropTableBefore = TRUE --> drop the table before inserting new values
        if incrementalFlag:
            ifExistsVal = 'append'
            logging.debug(f'writing data to staging table {tableName}, incrementalFlag={incrementalFlag}, appending new data to the existing table')
        else:
            ifExistsVal = 'replace'
            logging.debug(f'writing data to staging table {tableName}, incrementalFlag={incrementalFlag}, droping the table {tableName} and writing new data')

        
        # creating SQLAlchemy engine
        try:
            engine = create_engine(self.sqlConnStr)
        except SQLAlchemyError as e:
            print("An error occurred:", e)

        logging.debug(f'fetching the dtype dict for table {tableName}')
        entityDtype = self.getTableDtypedict(tableName)
        
        logging.debug(f' writing staging data to {"stg_"+tableName}')
        df.to_csv('output.csv', index=False)

        r = df.to_sql(
                        'stg_'+tableName,
                        con=engine
                        ,if_exists=ifExistsVal
                        ,dtype=entityDtype
                        ,index=False)
        logging.debug(f'{r} records were written to table {"stg_"+tableName}')
        
        return r
    

    def getPriorityEntities(self):
        '''
        description: The method queries the extraction configuration collection in the MongoDB for the entities that are marked as priority.

        parameters: None
        
        returns: list of dict {entityID: <entityID>, expand: <expand>}
        
        '''
        logging.debug('querying the metadata collection')

        db=priorityDataSource.metadataMongoDB
        collection = db[self.accountID]
        
        projection = {"_id": 1, "Desc": 1}
        result = list(collection.find({},projection))
        
        return result
    

    def getTableDtypedict(self,tableName):
        
        # create the inspector and connect it to the engine
        engine = create_engine(self.sqlConnStr)
        inspector = inspect(engine)

        # get the data types of the columns
        columns = inspector.get_columns(tableName)

        # create the dtype dict
        dtypedict = {column['name']: column['type'] for column in columns}
        return dtypedict
    


    