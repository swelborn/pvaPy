#!/usr/bin/env python

import curses
import argparse
import threading
import time
import json
import queue
import pvaccess as pva
import multiprocessing as mp
from ..utility.loggingManager import LoggingManager
from ..utility.objectUtility import ObjectUtility
from ..hpc.dataConsumer import DataConsumer

__version__ = pva.__version__

WAIT_TIME = 1.0
MIN_STATUS_UPDATE_PERIOD = 10.0
COMMAND_EXEC_DELAY = 0.1

GET_STATS_COMMAND = 'get_stats'
RESET_STATS_COMMAND = 'reset_stats'
CONFIGURE_COMMAND = 'configure'
STOP_COMMAND = 'stop'

class ConsumerController:

    CONSUMER_CONTROL_TYPE_DICT = {
        'consumerId' : pva.UINT,
        'objectTime' : pva.DOUBLE,
        'objectTimestamp' : pva.PvTimeStamp(),
        'command' : pva.STRING,
        'kwargs' : pva.STRING,
        'statusMessage' : pva.STRING
    }

    CONSUMER_STATUS_TYPE_DICT = {
        'consumerId' : pva.UINT,
        'objectId' : pva.UINT,
        'objectTime' : pva.DOUBLE,
        'objectTimestamp' : pva.PvTimeStamp(),
        'monitorStats' : {
            'nReceived' : pva.UINT,
            'receivedRate' : pva.DOUBLE,
            'nOverruns' : pva.UINT, 
            'overrunRate' : pva.DOUBLE
        },
        'queueStats' : {
            'nReceived' : pva.UINT,
            'nRejected' : pva.UINT,
            'nDelivered' : pva.UINT,
            'nQueued' : pva.UINT
        },
        'processorStats' : {
            'runtime' : pva.DOUBLE,
            'startTime' : pva.DOUBLE,
            'endTime' : pva.DOUBLE,
            'receivingTime' : pva.DOUBLE,
            'firstObjectTime' : pva.DOUBLE, 
            'lastObjectTime' : pva.DOUBLE,
            'firstObjectId' : pva.UINT,
            'lastObjectId' : pva.UINT,
            'nProcessed' : pva.UINT,
            'processedRate' : pva.DOUBLE,
            'nErrors' : pva.UINT,
            'errorRate' : pva.DOUBLE,
            'nMissed' : pva.UINT, 
            'missedRate' : pva.DOUBLE
        }
    }

    def __init__(self, args):
        self.screen = None
        if args.log_level:
            LoggingManager.setLogLevel(args.log_level)
            if args.log_file:
                LoggingManager.addFileHandler(args.log_file)
                self.screen = curses.initscr()
            else:
                LoggingManager.addStreamHandler()
        else:
            self.screen = curses.initscr()

        self.logger = LoggingManager.getLogger(self.__class__.__name__)
        self.args = args
        self.isDone = False
        self.statsObjectId = 0

    def controlCallback(self, pv):
        t = time.time()
        if 'command' not in pv:
            statusMessage = f'Ignoring invalid request (no command specified): {pv}'
            self.logger.warning(statusMessage)
            self.controlPvObject.set({'statusMessage' : statusMessage, 'objectTime' : t, 'objectTimestamp' : pva.PvTimeStamp(t)})
            return
        command = pv['command']
        self.logger.debug(f'Got command: {command}')
        if command == RESET_STATS_COMMAND:
            self.logger.info('Control channel: resetting consumer statistics')
            cTimer = threading.Timer(COMMAND_EXEC_DELAY, self.controlResetStats)
        elif command == GET_STATS_COMMAND:
            self.logger.info('Control channel: getting consumer statistics')
            cTimer = threading.Timer(COMMAND_EXEC_DELAY, self.controlGetStats)
        elif command == CONFIGURE_COMMAND:
            kwargs = ''
            if 'kwargs' not in pv:
                self.logger.debug('Empty keyword arguments string for the configure request')
            else:
                kwargs = pv['kwargs']
            self.logger.info(f'Control channel: configuring consumer with kwargs: {kwargs}')
            cTimer = threading.Timer(COMMAND_EXEC_DELAY, self.controlConfigure, args=[kwargs])
        elif command == STOP_COMMAND:
            self.logger.info(f'Control channel: stopping consumer')
            cTimer = threading.Timer(COMMAND_EXEC_DELAY, self.controlStop)
        else: 
            statusMessage = f'Ignoring invalid request (unrecognized command specified): {pv}'
            self.logger.warning(statusMessage)
            self.controlPvObject.set({'statusMessage' : statusMessage, 'objectTime' : t, 'objectTimestamp' : pva.PvTimeStamp(t)})
            return
        statusMessage = 'Command successful'
        self.controlPvObject.set({'statusMessage' : statusMessage, 'objectTime' : t, 'objectTimestamp' : pva.PvTimeStamp(t)})
        cTimer.start()

    def controlConfigure(self, kwargs):
        self.logger.debug(f'Configuring consumer {self.dataConsumer.getConsumerId()} with kwargs: {kwargs}')
        try:
            kwargs = json.loads(kwargs)
            self.logger.debug(f'Converted configuration kwargs string to JSON: {kwargs}')
        except Exception as ex:
            self.logger.debug(f'Cannot convert string {kwargs} from JSON: {ex}')
        try:
            self.dataConsumer.configure(kwargs)
            statusMessage = 'Configuration successful'
            self.logger.debug(statusMessage)
        except Exception as ex:
            statusMessage = f'Configuration failed: {ex}'
            self.logger.warning(statusMessage)
        self.controlPvObject['statusMessage'] = statusMessage

    def controlResetStats(self):
        self.logger.debug(f'Resetting stats for consumer {self.dataConsumer.getConsumerId()}')
        self.dataConsumer.resetStats()
        statusMessage = 'Stats reset successful'
        self.controlPvObject['statusMessage'] = statusMessage

    def controlGetStats(self):
        self.logger.debug(f'Getting stats for consumer {self.dataConsumer.getConsumerId()}')
        self.reportConsumerStats()
        statusMessage = 'Stats update successful'
        self.controlPvObject['statusMessage'] = statusMessage

    def controlStop(self):
        self.logger.debug(f'Stopping consumer {self.dataConsumer.getConsumerId()}')
        self.isDone = True
        statusMessage = 'Stop flag set'
        self.controlPvObject['statusMessage'] = statusMessage

    def createProcessor(self, consumerId, args):
        dataProcessor = None
        oidOffset = 1
        if args.oid_offset <= 0 and args.distributor_updates is not None:
            if args.n_distributor_sets > 1:
                self.logger.debug(f'Using oid offset appropriate for {args.n_distributor_sets} distributor client sets')
                if args.distributor_set is None:
                    raise pva.InvalidArgument(f'Specified number of distributor sets {args.n_distributor_sets} is greater than 1, but the actual distributor set name has not been set.')
                oidOffset = (args.n_distributor_sets-1)*int(args.distributor_updates)+1
            else:
                self.logger.debug('Using oid offset appropriate for a single distributor client set')
                oidOffset = (args.n_consumers-1)*int(args.distributor_updates)+1
            self.logger.debug(f'Determined oid offset: {oidOffset}')
       
        outputChannel =  args.output_channel
        if outputChannel == '_':
            outputChannel = f'{args.input_channel}:consumer:{consumerId}:output'
            self.logger.debug(f'Determined processor output channel name: {outputChannel}')

        if args.processor_file and args.processor_class:
            # Create config dict
            processorConfig = {}
            processorConfig['inputChannel'] = args.input_channel
            if args.processor_kwargs:
                processorConfig = json.loads(args.processor_kwargs)
            if not 'processorId' in processorConfig:
                processorConfig['processorId'] = consumerId
            if not 'processFirstUpdate' in processorConfig:
                processorConfig['processFirstUpdate'] = args.process_first_update
            if not 'objectIdField' in processorConfig:
                processorConfig['objectIdField'] = args.oid_field
            if not 'objectIdOffset' in processorConfig:
                processorConfig['objectIdOffset'] = oidOffset
            if not 'outputChannel' in processorConfig:
                processorConfig['outputChannel'] = outputChannel

            self.logger.debug(f'Using processor configuration: {processorConfig}')
            dataProcessor = ObjectUtility.createObjectInstanceFromFile(args.processor_file, 'dataProcessor', args.processor_class, processorConfig)
            if dataProcessor is not None:
                self.logger.debug(f'Created data processor {consumerId}: {dataProcessor}')
            else: 
                raise pva.InvalidArgument(f'Could not create data processor instance of class {args.processor_class} from file {args.processor_file}')
        return dataProcessor
            
    def createConsumer(self, consumerId, args):
        dataProcessor = self.createProcessor(consumerId, args)
            
        pvObjectQueue = None
        self.usingPvObjectQueue = False
        if args.consumer_queue_size >= 0:
            pvObjectQueue = pva.PvObjectQueue(args.consumer_queue_size)
            self.usingPvObjectQueue = True

        self.pvaServer = None
        self.statusChannel = args.status_channel
        if self.statusChannel == '_':
            self.statusChannel = f'{args.input_channel}:consumer:{consumerId}:status'
            self.logger.debug(f'Determined consumer status channel name: {self.statusChannel}')
        if self.statusChannel:
            self.pvaServer = pva.PvaServer()
            statusPvObject = pva.PvObject(self.CONSUMER_STATUS_TYPE_DICT, {'consumerId' : consumerId})
            self.pvaServer.addRecord(self.statusChannel, statusPvObject)
            self.logger.debug(f'Created consumer status channel: {self.statusChannel}')

        self.controlChannel = args.control_channel
        if self.controlChannel == '_':
            self.controlChannel = f'{args.input_channel}:consumer:{consumerId}:control'
            self.logger.debug(f'Determined consumer control channel name: {self.controlChannel}')
        if self.controlChannel:
            if not self.pvaServer:
                self.pvaServer = pva.PvaServer()
            # Keep reference to the control object so we can
            # update it
            self.controlPvObject = pva.PvObject(self.CONSUMER_CONTROL_TYPE_DICT, {'consumerId' : consumerId})
            self.pvaServer.addRecord(self.controlChannel, self.controlPvObject, self.controlCallback)
            self.logger.debug(f'Created consumer control channel: {self.controlChannel}')

        # Share PVA server if we have one
        if dataProcessor and self.pvaServer:
            dataProcessor.pvaServer = self.pvaServer

        self.dataConsumer = DataConsumer(consumerId, args.input_channel, providerType=args.input_provider_type, serverQueueSize=args.server_queue_size, distributorPluginName=args.distributor_plugin_name, distributorGroupId=args.distributor_group, distributorSetId=args.distributor_set, distributorTriggerFieldName=args.distributor_trigger, distributorUpdates=args.distributor_updates, distributorUpdateMode=None, pvObjectQueue=pvObjectQueue, dataProcessor=dataProcessor)
        return self.dataConsumer

    def startConsumers(self):
        self.createConsumer(self.args.consumer_id, args=self.args)
        self.dataConsumer.start()
        if self.pvaServer:
            self.pvaServer.start()
        self.logger.info(f'Started consumer {self.dataConsumer.getConsumerId()}')

    def reportConsumerStats(self, statsDict=None):
        if not statsDict:
            statsDict = self.getConsumerStats()
        consumerId = self.dataConsumer.getConsumerId()
        report = self.formatConsumerStats(consumerId, statsDict)
        if self.screen:
            self.screen.erase()
            self.screen.addstr(report)
            self.screen.refresh()
        else:
            print(report)

    def formatConsumerStats(self, consumerId, statsDict):
        now = time.time()
        report = 'consumer-{} @ {:.3f}s :\n'.format(consumerId, now)
        for k,v in statsDict.items():
            # Skip some keys and empty entries
            if not v:
                continue
            if k in ['objectId']:
                continue
            if type(v) == dict:
                report += '  {:15s}:'.format(k)
                for (k2,v2) in v.items():
                    report += ' {}'.format(self._formatDictEntry(k2,v2))
            else:
                report += '  {}'.format(self._formatDictEntry(k,v))
            report += '\n'
        # Remove last new line
        return report[0:-1] 

    def _formatDictEntry(self, k, v):
        if k.endswith('ime'):
            # anything ending with time or Time
            return '{}={:.3f}s'.format(k,v)
        elif k.endswith('ate'):
            # anything ending with rate or Rate
            return '{}={:.4f}Hz'.format(k,v)
        else:
            return '{}={}'.format(k,v)


    def getConsumerStats(self):
        statsDict = self.dataConsumer.getStats()
        self.statsObjectId += 1
        statsDict['objectId'] = self.statsObjectId
        t = time.time()
        if self.pvaServer:
            consumerId = self.dataConsumer.getConsumerId()
            statusObject = pva.PvObject(self.CONSUMER_STATUS_TYPE_DICT, {'consumerId' : consumerId, 'objectId' : self.statsObjectId, 'objectTime' : t, 'objectTimestamp' : pva.PvTimeStamp(t)})
            statusObject['monitorStats'] = statsDict.get('monitorStats', {})
            statusObject['queueStats'] = statsDict.get('queueStats', {})
            statusObject['processorStats'] = statsDict.get('processorStats', {})
            self.pvaServer.update(self.statusChannel, statusObject)
        return statsDict 

    def processPvUpdate(self, updateWaitTime):
        if self.usingPvObjectQueue:
            # This should be only done for a single consumer using a queue
            return self.dataConsumer.processFromQueue(updateWaitTime)
        return False

    def stopConsumers(self):
        self.logger.debug('Controller exiting')
        self.dataConsumer.stop()
        statsDict = self.dataConsumer.getStats()
        self.logger.info(f'Stopped consumer {self.dataConsumer.getConsumerId()}')
        if self.screen:
            curses.endwin()
        self.screen = None
        return statsDict

class MultiprocessConsumerController(ConsumerController):

    def __init__(self, args):
        ConsumerController.__init__(self, args)
        self.mpProcessMap = {}
        self.requestQueueMap = {}
        self.responseQueueMap = {}
        self.lastStatsObjectIdMap = {}

    def startConsumers(self):
        # Replace interrupt handler for worker processes
        # so we can exit cleanly
        import signal
        originalSigintHandler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        for consumerId in range(self.args.consumer_id, self.args.consumer_id+self.args.n_consumers):
            requestQueue = mp.Queue()
            self.requestQueueMap[consumerId] = requestQueue
            responseQueue = mp.Queue()
            self.responseQueueMap[consumerId] = responseQueue
            mpProcess = mp.Process(target=mpController, args=(consumerId, requestQueue, responseQueue, self.args,))
            self.mpProcessMap[consumerId] = mpProcess
            self.logger.debug(f'Starting consumer {consumerId}')
            mpProcess.start()
        signal.signal(signal.SIGINT, originalSigintHandler)

    def reportConsumerStats(self, combinedStatsDict=None):
        if not combinedStatsDict:
            combinedStatsDict = self.getConsumerStats()
        report = ''
        for consumerId,statsDict in combinedStatsDict.items():
            report += self.formatConsumerStats(consumerId, statsDict)
            report += '\n'
        if self.screen:
            self.screen.erase()
            self.screen.addstr(report)
            self.screen.refresh()
        else:
            # Remove extra newline character
            print(report[0:-1])

    def getConsumerStats(self):
        for consumerId in range(self.args.consumer_id, self.args.consumer_id+self.args.n_consumers):
            requestQueue = self.requestQueueMap[consumerId]
            try:
                requestQueue.put(GET_STATS_COMMAND, block=True, timeout=WAIT_TIME)
            except Exception as ex:
                self.logger.error(f'Cannot request stats from consumer {consumerId}: {ex}')
        statsDict = {}
        for consumerId in range(self.args.consumer_id, self.args.consumer_id+self.args.n_consumers):
            statsDict[consumerId] = {}
            lastStatsObjectId = self.lastStatsObjectIdMap.get(consumerId, 0)
            try:
                while True:
                    responseQueue = self.responseQueueMap[consumerId]
                    statsDict[consumerId] = responseQueue.get(block=True, timeout=WAIT_TIME)
                    statsObjectId = statsDict[consumerId].get('objectId', 0)
                    if statsObjectId != lastStatsObjectId:
                        self.lastStatsObjectIdMap[consumerId] = statsObjectId 
                        break
                    else:
                        self.logger.warning(f'Discarding stale stats received from consumer {consumerId}')
            except queue.Empty:
                self.logger.error(f'No stats received from consumer {consumerId}')
        return statsDict

    def processPvUpdate(self, updateWaitTime):
        return False

    def stopConsumers(self):
        for consumerId in range(self.args.consumer_id, self.args.consumer_id+self.args.n_consumers):
            requestQueue = self.requestQueueMap[consumerId]
            try:
                requestQueue.put(STOP_COMMAND, block=True, timeout=WAIT_TIME)
            except Exception as ex:
                self.logger.error(f'Cannot stop consumer {consumerId}: {ex}')
        statsDict = {}
        for consumerId in range(self.args.consumer_id, self.args.consumer_id+self.args.n_consumers):
            statsDict[consumerId] = {}
            try:
                responseQueue = self.responseQueueMap[consumerId]
                statsDict[consumerId] = responseQueue.get(block=True, timeout=WAIT_TIME)
            except queue.Empty:
                self.logger.error(f'No stats received from consumer {consumerId}')
        for consumerId in range(self.args.consumer_id, self.args.consumer_id+self.args.n_consumers):
            mpProcess = self.mpProcessMap[consumerId]
            mpProcess.join(WAIT_TIME)
            self.logger.info(f'Stopped consumer {consumerId}')
        if self.screen:
            curses.endwin()
            self.screen = None
        self.logger.debug('Controller exiting')
        return statsDict

class MpControllerRequestProcessingThread(threading.Thread):

    def __init__(self, controller, consumerId, requestQueue, responseQueue):
        threading.Thread.__init__(self)
        self.controller = controller
        self.consumerId = consumerId
        self.requestQueue = requestQueue
        self.responseQueue = responseQueue
        self.logger = LoggingManager.getLogger(f'rpThread-{self.consumerId}')

    def run(self):
        self.logger.debug(f'Request processing thread for consumer {self.consumerId} starting')
        while True:
            try:
                if self.controller.isDone:
                    self.logger.debug(f'Consumer {self.consumerId} is done, request processing thread is exiting')
                    break

                # Check for new request
                try:
                    request = self.requestQueue.get(block=True, timeout=WAIT_TIME)
                    self.logger.debug(f'Received request: {request}')
                    if request == STOP_COMMAND:
                        self.controller.isDone = True
                        self.logger.debug(f'Consumer {self.consumerId} received stop command, request processing thread is exiting')
                        break
                    elif request == GET_STATS_COMMAND:
                        statsDict = self.controller.getConsumerStats()
                        try:
                            self.responseQueue.put(statsDict, block=False)
                        except Exception as ex:
                            self.logger.error(f'Consumer {consumerId} cannot report stats: {ex}')
                except queue.Empty:
                    pass

            except Exception as ex:
                self.logger.error(f'Request processing error: {ex}')

        self.logger.debug(f'Request processing thread for consumer {self.consumerId} exited')
   
def mpController(consumerId, requestQueue, responseQueue, args):
    controller = ConsumerController(args)
    logger = LoggingManager.getLogger(f'mpController-{consumerId}')
    dataConsumer = controller.createConsumer(consumerId, args)
    dataConsumer.start()

    # Process controller requests in a separate thread
    rpThread = MpControllerRequestProcessingThread(controller, consumerId, requestQueue, responseQueue)
    rpThread.start()

    waitTime = WAIT_TIME
    while True:
        try:
            if controller.isDone:
                logger.debug(f'Consumer {consumerId} is done')
                break

            now = time.time()
            wakeTime = now+waitTime

            # Try to process object
            delay = 0
            hasProcessedObject = controller.processPvUpdate(waitTime)
            if not hasProcessedObject:
                # Determine if we can wait
                delay = wakeTime-time.time()
                if delay > 0:
                    time.sleep(delay)

        except Exception as ex:
            logger.error(f'Processing error: {ex}')

    dataConsumer.stop()
    statsDict = controller.getConsumerStats()
    try:
        responseQueue.put(statsDict, block=True, timeout=WAIT_TIME)
    except Exception as ex:
        logger.error(f'Consumer {consumerId} cannot report stats on exit: {ex}')
    time.sleep(WAIT_TIME)

def main():
    parser = argparse.ArgumentParser(description='PvaPy HPC Consumer utility. It can be used for receiving and processing data using specified implementation of the data processor interface.')
    parser.add_argument('-v', '--version', action='version', version='%(prog)s {version}'.format(version=__version__))
    parser.add_argument('-id', '--consumer-id', dest='consumer_id', type=int, default=1, help='Consumer id (default: 1). If spawning multiple consumers, this option will be interpreted as the first consumer id; for each subsequent consumer id will be increased by 1. Note that consumer id is used for naming various PVA channels, so care must be taken when multiple consumer processes are running independently of each other.')
    parser.add_argument('-nc', '--n-consumers', type=int, dest='n_consumers', default=1, help='Number of consumers to instantiate (default: 1). If > 1, multiprocessing module will be used for receiving and processing data in separate processes.')
    parser.add_argument('-ic', '--input-channel', dest='input_channel', required=True, help='Input PV channel name.')
    parser.add_argument('-ipt', '--input-provider-type', dest='input_provider_type', default='pva', help='Input PV channel provider type, it must be either "pva" or "ca" (default: pva).')
    parser.add_argument('-oc', '--output-channel', dest='output_channel', default=None, help='Output PVA channel name (default: None). If specified, this channel can be used for publishing processing results. The value of "_" indicates that the output channel name will be set to "<input channel>:consumer:<consumer id>:output". Note that this parameter is ignored if processor kwargs dictionary contains "outputChannel" key.')
    parser.add_argument('-sc', '--status-channel', dest='status_channel', default=None, help='Status PVA channel name (default: None). If specified, this channel will provide consumer status. The value of "_" indicates that the status channel name will be set to "<input channel>:consumer:<consumer id>:status".')
    parser.add_argument('-cc', '--control-channel', dest='control_channel', default=None, help='Control channel name (default: None). If specified, this channel can be used to control consumer configuration and processing. The value of "_" indicates that the control channel name will be set to "<input channel>:consumer:<consumer id>:control". The control channel object has two strings: command and kwargs. The only allowed values for the command string are: "configure", "reset_stats", "get_stats" and "stop". The configure command is used to allow for runtime configuration changes; in this case the keyword arguments string should be in json format to allow data consumer to convert it into python dictionary that contains new configuration. For example, sending configuration dictionary via pvput command might look like this: pvput input_channel:consumer:2:control \'{"command" : "configure", "kwargs" : "{\\"x\\":100}"}\'. Note that system parameters that can be modified at runtime are the following: "consumerQueueSize" (only if consumer queue has been configured at the start), "processFirstUpdate" (affects consumer behavior after resetting stats), and "objectIdOffset" (may be used to adjust offset if consumers have been added or removed from processing). The reset_stats command will cause consumer to reset it statistics data, the get_stats will force statistics data update, and the stop command will result in consumer process exiting; for all these commands kwargs string is not needed.')
    parser.add_argument('-sqs', '--server-queue-size', type=int, dest='server_queue_size', default=0, help='Server queue size (default: 0); this setting will increase memory usage on the server side, but may help prevent missed PV updates.')
    parser.add_argument('-cqs', '--consumer-queue-size', type=int, dest='consumer_queue_size', default=-1, help='Consumer queue size (default: -1); if >= 0, PvObjectQueue will be used for receving PV updates (value of zero indicates infinite queue size).')
    parser.add_argument('-pf', '--processor-file', dest='processor_file', default=None, help='Full path to the python file containing processor class.')
    parser.add_argument('-pc', '--processor-class', dest='processor_class', default=None, help='Name of the class located in the processor file that will be processing PV updates; it should be initialized with a dictionary and must implement the "process(self, pv)" method.')
    parser.add_argument('-pk', '--processor-kwargs', dest='processor_kwargs', default=None, help='JSON-formatted string that can be converted into dictionary and used for initializing processor object.')
    parser.add_argument('-oo', '--oid-offset', type=int, dest='oid_offset', default=0, help='This parameter determines by how much object id should change between the two PV updates, and is used for determining the number of missed PV updates (default: 0). This parameter is ignored if processor kwargs dictionary contains "objectIdOffset" key, and should be modified only if data distributor plugin will be distributing data between multiple clients, in which case it should be set to "(<nConsumers>-1)*<nUpdates>+1" for a single client set, or to "(<nSets>-1)*<nUpdates>+1" for multiple client sets. Values <= 0 will be replaced with the appropriate value depending on the number of client sets specified. Note that this relies on using the same value for the --n-distributor-sets when multiple instances of this command are running separately.')
    parser.add_argument('-of', '--oid-field', dest='oid_field', default='uniqueId', help='PV update id field used for calculating data processor statistics (default: uniqueId). This parameter is ignored if processor kwargs dictionary contains "objectIdField" key.')
    parser.add_argument('-pfu', '--process-first-update', dest='process_first_update', default=False, action='store_true', help='Process first PV update (default: False). This parameter is ignored if processor kwargs dictionary contains "processFirstUpdate" key.')
    parser.add_argument('-dpn', '--distributor-plugin-name', dest='distributor_plugin_name', default='pydistributor', help='Distributor plugin name (default: pydistributor).')
    parser.add_argument('-dg', '--distributor-group', dest='distributor_group', default=None, help='Distributor client group that application belongs to (default: None). This parameter should be used only if data distributor plugin will be distributing data between multiple clients. Note that different distributor groups are completely independent of each other.')
    parser.add_argument('-ds', '--distributor-set', dest='distributor_set', default=None, help='Distributor client set that application belongs to within its group (default: None). This parameter should be used only if data distributor plugin will be distributing data between multiple clients. Note that all clients belonging to the same set receive the same PV updates. If set id is not specified (i.e., if a group does not have multiple sets of clients), a PV update will be distributed to only one client.')
    parser.add_argument('-dt', '--distributor-trigger', dest='distributor_trigger', default=None, help='PV structure field that data distributor uses to distinguish different channel updates (default: None). This parameter should be used only if data distributor plugin will be distributing data between multiple clients. In case of, for example, area detector applications, the "uniqueId" field would be a good choice for distinguishing between the different frames.')
    parser.add_argument('-du', '--distributor-updates', dest='distributor_updates', default=None, help='Number of sequential PV channel updates that a client (or a set of clients) will receive (default: None). This parameter should be used only if data distributor plugin will be distributing data between multiple clients.')
    parser.add_argument('-nds', '--n-distributor-sets', type=int, dest='n_distributor_sets', default=1, help='Number of distributor client sets (default: 1). This setting is used to determine appropriate value for the processor object id offset in case where multiple instances of this command are running separately for different client sets. If distributor client set is not specified, this setting is ignored.')
    parser.add_argument('-rt', '--runtime', type=float, dest='runtime', default=0, help='Server runtime in seconds; values <=0 indicate infinite runtime (default: infinite).')
    parser.add_argument('-rp', '--report-period', type=float, dest='report_period', default=0, help='Statistics report period for all consumers in seconds; values <=0 indicate no reporting (default: 0).')
    parser.add_argument('-ll', '--log-level', dest='log_level', help='Log level; possible values: DEBUG, INFO, WARNING, ERROR, CRITICAL. If not provided, there will be no log output.')
    parser.add_argument('-lf', '--log-file', dest='log_file', help='Log file.')

    args, unparsed = parser.parse_known_args()
    if len(unparsed) > 0:
        print('Unrecognized argument(s): {}'.format(' '.join(unparsed)))
        exit(1)

    if args.n_consumers == 1:
        controller = ConsumerController(args)
    else:
        controller = MultiprocessConsumerController(args)
    controller.startConsumers()
    startTime = time.time()
    lastReportTime = startTime
    lastStatusUpdateTime = startTime
    waitTime = WAIT_TIME
    while True:
        try:
            now = time.time()
            wakeTime = now+waitTime
            if controller.isDone:
                break
            if args.runtime > 0:
                runtime = now - startTime
                if runtime > args.runtime:
                    break
            if args.report_period > 0 and now-lastReportTime > args.report_period:
                lastReportTime = now
                lastStatusUpdateTime = now
                controller.reportConsumerStats()

            if args.status_channel and now-lastStatusUpdateTime > MIN_STATUS_UPDATE_PERIOD:
                lastStatusUpdateTime = now
                controller.getConsumerStats()

            hasProcessedObject = controller.processPvUpdate(waitTime)
            if not hasProcessedObject:
                # Check if we need to sleep
                delay = wakeTime-time.time()
                if delay > 0:
                    time.sleep(delay)
        except KeyboardInterrupt as ex:
            break

    statsDict = controller.stopConsumers()
    controller.reportConsumerStats(statsDict)
    # Allow clients monitoring various channels to get last update
    time.sleep(WAIT_TIME)

if __name__ == '__main__':
    main()
