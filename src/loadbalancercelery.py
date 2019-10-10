from __future__ import absolute_import
from celery import Celery
# celery
from celery.utils.log import get_task_logger


import json
from MQTTPubSub import MQTTPubSub
import pymongo
import time
import datetime
import os
import sys
# LBS Params


'''
    TODO:
        1. Validation at mongodb
        2. Make streams doc dist_ip an array
           or comprimise with redundant doc
'''


''' Celery app '''
app = Celery('loadbalancercelery', backend="redis://", broker="redis://")

'''Logger '''
logger = get_task_logger(__name__)


''' mongo initializations '''
mongoclient = pymongo.MongoClient('mongodb://localhost:27017/')
mongoDB = mongoclient["ALL_Streams"]
col2 = mongoDB["Distribution_Servers"]
col3 = mongoDB["Streams"]
col4 = mongoDB["Ffmpeg_Procs"]
col5 = mongoDB["Users"]
col6 = mongoDB["Archives"]

# Update Origin, Dists and Streams


class Table():
    '''
        Perform operations on mongo table
        Args:
            mc: Mongo DB
    '''

    def __init__(self, mongoDB, name):
        self.collection = mongoDB[name]

    def insertOne(self, doc):
        res = self.collection.insert_one(doc)
        if(res.inserted_id is not None):
            return 1
        else:
            return 0

    def update(self, key, doc):
        res = self.collection.update_one(key, {"$set": doc}, upsert=True)
        return res.modified_count()

    def findOne(self, doc, args=None):
        res = self.collection.find_one(doc, {"_id": 0})
        return res

    def findAll(self, doc=None):
        if (doc is not None):
            res = self.collection.find(doc, {"_id": 0})
            return res
        else:
            return self.collection.find({}, {"_id": 0})

    def delete(self, doc):
        self.collection.delete_one(doc)

    def deleteMany(self, doc):
        self.collection.delete_many(doc)

    def count(self):
        return self.collection.count()


'''
    {origin_id: string, origin_ip: string[uri], num_clients: int}
'''
originTable = Table(mongoDB, "originTable")

'''
    {cmd: string, from_ip: string, stream_ip: string,
     to_ip: string, rtsp_cmd: string}
    col4
'''
ffmpegProcsTable = Table(mongoDB, "ffmpegProcsTable")

'''
    {stream_id: string, stream_ip: string[uri], 
     origin_ip: string[uri], dist_ip: string[uri]
     status: enum[onboadrding, deleting, active, down]
     }
    col3
'''
streamsTable = Table(mongoDB, "streams")

'''
    {dist_id: string, dist_ip: string[uri], num_clients: int}
'''
distTable = Table(mongoDB, "distTable")


def choose_origin(stream):
    ''' Algorithm to choose origin server on which to onboard a stream '''
    ''' TODO: Use some info of stream '''
    origins = originTable.findAll()
    bestOrigin = {}
    bestNumClients = 100
    for origin in origins:
        if (origin["num_clients"] < bestNumClients):
            bestOrigin = origin
    return bestOrigin


def choose_dist(stream):
    ''' Algorithm to choose dist server on which to publish a stream '''
    ''' TODO: Use some info of stream '''
    dists = distTable.findAll()
    bestDist = {}
    bestNumClients = 100
    for dist in dists:
        if (dist["num_clients"] < bestNumClients):
            bestDist = dist
    return bestDist


@app.task
def GetOrigins():
    '''
        Trigger: celeryLBmain.py
        Handles: Show all origin servers
        Response: HTTPServer.py
    '''
    res = json.dumps(originTable.findAll())
    return {"topic": "lbsresponse/origin/all", "msg": res}


@app.task
def InsertOrigin(msg):
    '''
        Input: {origin_id: string, origin_ip: string[uri]}
        Trigger: celeryLBmain.py
        Handles: Origin insertion requests
        Response: HTTPServer.py
    '''
    logger.info("Inserting Origin")
    msg = json.loads(msg)
    ret = originTable.insertOne(msg)
    if ret == 1:
        logger.info("Added origin ", msg["origin_id"])
        return {"topic": "lbsresponse/origin/add", "msg": True}
    else:
        logger.info("Origin already present", msg["origin_ip"])
        return {"topic": "lbsresponse/origin/add", "msg": False}


@app.task
def OriginStat(msg):
    '''
        Input: {origin_id: string, num_clients: number}
        Trigger: celeryLBmain.py
        Handles: Update num_clients
        Response: None
    '''
    msg = json.loads(msg)
    originTable.update({"origin_id": msg["origin_id"]},
                       {"num_clients": msg["num_clients"]})


@app.task
def DeleteOrigin(msg):
    '''
        Input: {origin_id: string}
        Trigger: celeryLBmain.py
        Handles: Origin deletion requests
        Response: HTTPServer.py
        TODO: Kill origin streams
    '''
    logger.info("Deleting Origin")
    ret = originTable.delete({"origin_id": msg["origin_id"]})
    if ret == 1:
        logger.info("Deleted origin ", msg["origin_id"])
        ffmpegProcsTable.deleteMany({"to_id": msg["origin_id"]})
        ffmpegProcsTable.deleteMany({"origin_id": msg["origin_id"]})
        streamsTable.deleteMany({"from_id": msg["origin_id"]})
        logger.info("Origin Deleted----> ID:" + " ID:"+str(msg["origin_id"]))
        return [{"topic": "lbsresponse/origin/del", "msg": True},
                {"topic": "origin/ffmpeg/killall", "msg": msg}]
    else:
        return {"topic": "lbsresponse/origin/del", "msg": False}


@app.task
def UpdateOriginStream(msg):
    '''
        Input: {cmd: string, from_ip: string, stream_id: string,
                to_ip: string, rtsp_cmd: string}
        Trigger: OriginCelery.py
        Handles: adding ffmpeg stream to db once
                 it's added at the origin server
    '''
    msg = json.loads(msg)
    logger.info(str(msg["stream_id"]) +
                " stream has been started to origin " + str(msg["to_ip"]))
    ffmpegProcsTable.insertOne(msg)
    streamsTable.update({"stream_id": msg["stream_id"]},
                        {"$set": {"origin_ip": msg["to_ip"]}})
    time.sleep(0.1)
    return 0


@app.task
def ReqAllOriginStreams(msg):
    '''
        Input: {origin_id: string}
        Trigger: OriginCelery.py
        Handles: show all streams belonging to an origin ip
                 it's added at the origin server
    '''
    msg = json.loads(msg)
    streams = streamsTable.findAll(msg)
    resp = {"origin_id": msg["origin_id"], "stream_list": streams}
    return {"topic": "lb/request/origin/streams", "msg": json.dumps(resp)}


@app.task
def ReqAllDistStreams(msg):
    '''
        Input: {dist_id: string}
        Trigger: OriginCelery.py
        Handles: show all streams belonging to a dist id
                 after it's added at the dist server
    '''
    msg = json.loads(msg)
    streams = streamsTable.findAll(msg)
    resp = {"dist_id": msg["dist_id"], "stream_list": streams}
    return {"topic": "lb/request/dist/streams", "msg": json.dumps(resp)}


@app.task
def InsertDist(msg):
    '''
        Input: {dist_id: string, dist_ip: string[uri]}
        Trigger: celeryLBmain.py
        Handles: Dist insertion requests
        Response: HTTPServer.py
    '''
    logger.info("Inserting Dist")
    msg = json.loads(msg)
    ret = distTable.insertOne(msg)
    if ret == 1:
        logger.info("Added dist", msg["dist_id"])
        return {"topic": "lbsresponse/dist/add", "msg": True}
    else:
        logger.info("Dist already present", msg["dist_ip"])


@app.task
def DeleteDist(msg):
    '''
        Input: {dist_id: string}
        Trigger: celeryLBmain.py
        Handles: Dist deletion requests
        Response: HTTPServer.py
    '''
    logger.info("Deleting Dist")
    msg = json.loads(msg)
    ret = distTable.delete({"dist_id": msg["dist_id"]})
    killlist = []
    if ret == 1:
        logger.info("Deleted dist ", msg["dist_id"])
        killlist = ffmpegProcsTable.findAll({"dist_id": msg["dist_id"]})
        ffmpegProcsTable.deleteMany({"to_id": msg["dist_id"]})
        ffmpegProcsTable.deleteMany({"dist_id": msg["dist_id"]})
        streamsTable.deleteMany({"from_id": msg["dist_id"]})
        return [{"topic": "lbsresponse/dist/del", "msg": True},
                {"topic": "origin/ffmpeg/kill", "msg": killlist}]
    else:
        return {"topic": "lbsresponse/dist/del", "msg": False}


@app.task
def GetDists():
    '''
        Trigger: celeryLBmain.py
        Handles: Show all dist servers
        Response: HTTPServer.py
    '''
    res = json.dumps(distTable.findAll())
    return {"topic": "lbsresponse/dist/all", "msg": res}


@app.task
def DistStat(msg):
    '''
        Input: {dist_id: string, dist_clients: number}
        Trigger: celeryLBmain.py
        Handles: Update num_clients
        Response: None
    '''
    msg = json.loads(msg)
    distTable.update({"dist_id": msg["dist_id"]},
                     {"num_clients": msg["num_clients"]})


@app.task()
def OriginFfmpegDistPush(msg):
    '''
        Input: {stream_id: string, cmd: string, rtsp_cmd: string,
                from_ip: string, to_ip: string}

        Trigger: celeryLBmain.py
        Handles: Inserts origin stream info into db upon succesful
                 pull from camera
        Response: HTTPServer
    '''
    msg = json.loads(msg)
    logger.info(msg)
    logger.info(msg["stream_id"]+" stream push has been started from origin " +
                msg["from_ip"]+" to distribution "+msg["to_ip"])

    ffmpegProcsTable.insertOne({"cmd": msg["cmd"], "to_ip": msg["to_ip"],
                                "from_ip": msg["from_ip"],
                                "stream_id": msg["stream_id"],
                                "rtsp_cmd": msg["rtsp_cmd"]})

    streamsTable.update({"stream_id": msg["stream_id"],
                         "origin_ip": msg["from_ip"]},
                        {"$set": {"dist_ip": msg["to_ip"]}})
    logger.info(str(msg["cmd"].split()[-2]))
    time.sleep(0.1)
    return {"topic": "lbsresponse/rtmp", "msg": str(msg["cmd"].split()[-2])}


@app.task
def OriginFfmpegRespawn(msg):
    '''
        Input: {stream_id: string}
        Trigger: celeryLBmain.py
        Handles: Respawn origin stream
        Response: HTTPServer.py
        TODO: Respawn based on logic
    '''
    msg = json.loads(msg)
    logger.info(str(msg)+" should come here only when missing becomes active")
    return {"topic": "origin/ffmpeg/respawn", "msg": msg}


@app.task
def OriginFFmpegDistRespawn(msg):
    '''
        Input: {stream_id: string}
        Trigger: celeryLBmain.py
        Handles: Respawns origin to distribution published stream
        Response: HTTPServer.py
        TODO: Respawn based on logic
    '''
    msg = json.loads(msg)
    logger.info("Respawning", msg["stream_id"])
    return {"topic": "origin/ffmpeg/dist/respawn", "msg": msg}


@app.task
def InsertStream(msg):
    '''
        Input: {stream_id: string, stream_ip: string}
        Trigger: celeryLBmain.py
        Handles: add a stream to origin server
        Response: HTTPServer.py
    '''
    msg = json.loads(msg)

    if originTable.count() == 0:
        logger.info("No Origin Server Present")
        return 0

    streams = streamsTable.findAll(msg)
    origin = choose_origin(streams)
    stream = streamsTable.findOne(msg)
    if len(stream) is 0:
        streamsTable.insertOne({"stream_ip": msg["stream_ip"],
                                "stream_id": msg["stream_id"],
                                "origin_ip": origin["origin_ip"],
                                "dist_ip": ""})
        logger.info("Added stream ", msg["stream_id"],
                    " to ", origin["origin_id"])
        out = {"origin_ip": origin["origin_ip"],
               "stream_id": msg["stream_id"],
               "stream_ip": msg["stream_ip"]}
        return [{"topic": "lbsresponse/stream/add", "msg": True},
                {"topic": "origin/ffmpeg/stream/spawn", "msg": out},
                {"topic": "origin/ffmpeg/stream/stat/spawn", "msg": out}]
    else:
        logger.warning("Stream ", msg["stream_id"],
                       " to ", origin["origin_id"], " already present")
        return {"topic": "lbsresponse/stream/add", "msg": False}


@app.task
def DeleteStream(msg):
    '''
        Input: {stream_id: string}
        Trigger: celeryLBmain.py
        Handles: delete a stream of the origin server
        Response: HTTPServer.py
    '''
    msg = json.loads(msg)
    killlist = []
    streams = streamsTable.findAll(msg)
    logger.info("Deleting ", msg["stream_id"], " from", )
    if len(streams) is 0:
        logger.info("Stream ", msg["stream_id"], " not found")
        return {"topic": "lbsresponse/stream/del", "msg": False}
    else:
        killlist = ffmpegProcsTable.findAll(msg)
        streamsTable.delete(msg)
        ffmpegProcsTable.deleteMany(msg)
        return [{"topic": "lbsresponse/stream/del", "msg": True},
                {"topic": "origin/ffmpeg/kill", "msg": killlist}]


@app.task
def RequestStream(msg):
    '''
        Input: {stream_id: string}
        Trigger: celeryLBmain.py
        Handles: Gives the user a stream from the distribution server
        Response: HTTPServer.py
    '''
    msg = json.loads(msg)

    stream = streamsTable.findOne(msg)
    ffproc = ffmpegProcsTable.findOne(msg)

    if (len(stream) is 0):
        ''' Steram not present at the origin server '''
        logger.error("Stream not present")
        return {"topic": "lbsresponse/rtmp",
                "msg": json.dumps({"info": "unavailable"})}

    if (len(stream) is not 0) and (len(ffproc) is 0):
        ''' Stream registered but origin ffmpeg processes missing '''
        return {"topic": "lbsresponse/rtmp",
                "msg": json.dumps({"info": "processing"})}

    if (len(stream) is not 0) and (len(ffproc) is 0):
        ''' Stream registered but dist ffmpeg processes missing '''

        dist = choose_dist(stream)
        resp = {"origin_id": stream["origin_id"], "dist_id": dist["dist_id"],
                "stream_id": stream["stream_id"],
                "stream_ip": stream["stream_ip"]}
        userresp = {"stream_id": stream["stream_id"], "rtmp": ffproc["cmd"],
                    "hls": "http://" + ffproc["to_ip"] +
                           ":8080/hls/" + stream["stream_id"] + ".m3u8",
                    "rtsp": ffproc["rtsp_cmd"], "info": "active"}

        return [{"topic": "lbsresponse/rtmp",
                 "msg": json.dumps(userresp)},
                {"topic": "origin/ffmpeg/dist/spawn",
                 "msg": json.dumps(resp)},
                {"topic": "dist/ffmpeg/stream/stat/spawn",
                 "msg": json.dumps(resp)},
                ]

    else:
        ''' All required conditions to send link are met '''
        logger.info("Stream ", msg["stream_id"], " already present")
        userresp = {"stream_id": msg["stream_id"],
                    "rtmp": ffproc["cmd"],
                    "hls": "http://" + ffproc["to_ip"] +
                           ":8080/hls/" + msg["stream_id"] + ".m3u8",
                           "rtsp": ffproc["rtsp_cmd"]}
        return {"topic": "lbsresponse/rtmp", "msg": json.dumps(userresp)}


@app.task
def GetStreams():
    '''
        Input: {}
        Trigger: celeryLBmain.py
        Handles: Shows all stream available
        Response: HTTPServer.py
    '''
    streams = streamsTable.findAll()
    return {"topic": "lbsresponse/stream/all", "msg": json.dumps(streams)}


@app.task
def ArchiveAdd(msg):
    '''
        Input: {}
        Trigger: celeryLBmain.py
        Handles: Add an archive
        Response: HTTPServer.py
    '''
    msg = json.loads(msg)
    logger.info("Adding archive")
    msg["stream_ip"] = col3.find_one(
        {"stream_id": msg["stream_id"]})["stream_ip"]
    msg["origin_ip"] = col3.find_one(
        {"stream_id": msg["stream_id"]})["origin_ip"]

    archivedstreams, archivedjobs = search_archives()
    if msg["job_id"] not in archivedjobs:
        col6.insert_one(
            {"Job_ID": msg["job_id"], "stream_id": msg["stream_id"]})
        return [{"topic": "lbsresponse/archive/add", "msg": True}, {"topic": "origin/ffmpeg/archive/add", "msg": msg}]
        logger.info(str(msg)+" archiving this......")
    else:
        return {"topic": "lbsresponse/archive/add", "msg": False}


'''
    To be refactored
'''




@app.task
def GetArchives():
    msg = {}
    streamarch, jobarch = search_archives()
    for i in range(len(streamarch)):
        msg[streamarch[i]] = jobarch[i]
    return {"topic": "lbsresponse/archive/all", "msg": msg}
















@app.task
def ArchiveDel(archive_stream_del):
    msg = json.loads(archive_stream_del[1])
    logger.info(msg)
    msg["stream_ip"] = col3.find_one(
        {"stream_id": msg["stream_id"]})["stream_ip"]
    msg["origin_ip"] = col3.find_one(
        {"stream_id": msg["stream_id"]})["origin_ip"]
    archivedstreams, archivedjobs = search_archives()
    if msg["job_id"] not in archivedjobs:
        return {"topic": "lbsresponse/archive/del", "msg": False}
    else:
        logger.info(str(msg)+"  deleting this archive......")
        col6.delete_one({"Job_ID": msg["job_id"]})
        return [{"topic": "lbsresponse/archive/del", "msg": True}, {"topic": "origin/ffmpeg/archive/delete", "msg": msg}]


@app.task
def GetUsers():
    usernames = search_users()
    return {"topic": "lbsresponse/user/all", "msg": usernames}



@app.task
def AddUser(user_add):
    msg = json.loads(user_add[1])
    if col5.count() == 0:
        col5.insert_one({"User": msg["User"], "Password": msg["Password"]})
        return {"topic": "lbsresponse/user/add", "msg": True}
    else:
        usernames = search_users()
        if msg["User"] not in usernames:
            col5.insert_one({"User": msg["User"], "Password": msg["Password"]})
            return {"topic": "lbsresponse/user/add", "msg": True}
        else:
            return {"topic": "lbsresponse/user/add", "msg": False}


@app.task
def DelUser(user_del):
    msg = json.loads(user_del[1])
    if col5.count() == 0:
        return{"topic": "lbsresponse/user/del", "msg": False}
    else:
        usernames = search_users()
        if msg["User"] not in usernames:
            return{"topic": "lbsresponse/user/del", "msg": False}
        else:
            count = 0
            for i in col5.find():
                if i["User"] == msg["User"]:
                    if i["Password"] == msg["Password"]:
                        col5.delete_one({"User": msg["User"]})
                        return {"topic": "lbsresponse/user/del", "msg": True}
                    else:
                        return {"topic": "lbsresponse/user/del", "msg": False}


@app.task
def VerifyUser(verify_user):
    print("Verifying ")
    print(verify_user)
    msg = json.loads(verify_user[1])
    if col5.count != 0:
        for i in col5.find():
            if i["User"] == msg["User"]:
                if i["Password"] == msg["Password"]:
                    return {"topic": "lbsresponse/verified", "msg": True}
    return {"topic": "lbsresponse/verified", "msg": False}
