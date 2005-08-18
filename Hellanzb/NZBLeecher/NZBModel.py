"""

NZBModel - Representations of the NZB file format in memory

(c) Copyright 2005 Philip Jenvey
[See end of file]
"""
import gc, os, re, stat, time, Hellanzb
from sets import Set
from threading import Lock, RLock
from twisted.internet import reactor
from xml.sax import make_parser, SAXParseException
from xml.sax.handler import ContentHandler, feature_external_ges, feature_namespaces
from Hellanzb.Core import shutdown
from Hellanzb.Daemon import handleNZBDone
from Hellanzb.Log import *
from Hellanzb.NZBLeecher.ArticleDecoder import assembleNZBFile, parseArticleData, \
    setRealFileName, tryFinishNZB
from Hellanzb.Util import archiveName, getFileExtension, PriorityQueue, PoolsExhausted, TooMuchWares
from Queue import Empty

__id__ = '$Id$'

def validWorkingFile(file, overwriteZeroByteFiles = False):
    """ Determine if the specified file is a valid WORKING_DIR file that will be checked
    against the NZB file currently being parsed (i.e. a valid working dir file will not be
    overwritten by the ArticleDecoder """
    if not os.path.isfile(file):
        return False

    # Overwrite 0 byte segment files if specified
    if 0 == os.stat(file)[stat.ST_SIZE] and overwriteZeroByteFiles:
        #debug('Will overwrite 0 byte segment file: ' + file)
        # FIXME: store these 0 byte files in a list, when we encounter a segment file
        # that matches one of these, we will tell the user we're overwriting the 0
        # byte file. FIXME: this should then also work for overwriting 0 byte on disk
        # NZBFiles
        return False
    
    return True

segmentEndRe = re.compile(r'^segment\d{4}$')
def segmentsNeedDownload(segmentList, overwriteZeroByteSegments = False):
    """ Faster version of needsDownload for multiple segments that do not have their real file
    name (for use by the Queue).

    When an NZB is loaded and parsed, NZB<file>s not found on disk at the time of parsing
    are marked as needing to be downloaded. (An easy first pass of figuring out exactly
    what needs to be downloaded).

    This function is the second pass. It takes all of those NZBFiles that need to be
    downloaded's child NZBSegments and scans the disk, detecting which segments are
    already on disk and can be skipped
    """
    # Arrange all WORKING_DIR segment's filenames in a list. Key this list by segment
    # number in a map. Loop through the specified segmentList, doing a subject.find for
    # each segment filename with a matching segment number

    onDiskSegmentsByNumber = {}
    
    needDlFiles = Set() # for speed while iterating
    needDlSegments = []
    onDiskSegments = []

    # Cache all WORKING_DIR segment filenames in a map of lists
    for file in os.listdir(Hellanzb.WORKING_DIR):
        if not validWorkingFile(Hellanzb.WORKING_DIR + os.sep + file,
                                overwriteZeroByteSegments):
            continue
        
        ext = getFileExtension(file)
        if ext != None and segmentEndRe.match(ext):
            segmentNumber = int(ext[-4:])
            
            if onDiskSegmentsByNumber.has_key(segmentNumber):
                segmentFileNames = onDiskSegmentsByNumber[segmentNumber]
            else:
                segmentFileNames = []
                onDiskSegmentsByNumber[segmentNumber] = segmentFileNames

            # cut off .segmentXXXX
            fileNoExt = file[:-12]
            segmentFileNames.append(fileNoExt)

    # Determine if each segment needs to be downloaded
    for segment in segmentList:

        if not onDiskSegmentsByNumber.has_key(segment.number):
            # No matching segment numbers, obviously needs to be downloaded
            needDlSegments.append(segment)
            needDlFiles.add(segment.nzbFile)
            continue

        segmentFileNames = onDiskSegmentsByNumber[segment.number]
        
        foundFileName = None
        for segmentFileName in segmentFileNames:

            # We've matched to our on disk segment if we:
            # a) find that on disk segment's file name in our potential segment's subject
            # b) match that on disk segment's file name to our potential segment's temp
            # file name (w/ .segmentXXXX cutoff)
            if segment.nzbFile.subject.find(segmentFileName) > -1 or \
                    segment.getTempFileName()[:-12] == segmentFileName:
                foundFileName = segmentFileName
                # make note that this segment doesn't have to be downloaded
                segment.nzbFile.todoNzbSegments.remove(segment)
                break

        if not foundFileName:
            needDlSegments.append(segment)
            needDlFiles.add(segment.nzbFile)
        else:
            if segment.number == 1 and foundFileName.find('hellanzb-tmp-') != 0:
                # HACK: filename is None. so we only have the temporary name in
                # memory. since we didnt see the temporary name on the filesystem, but
                # we found a subject match, that means we have the real name on the
                # filesystem. In the case where this happens, and we are segment #1,
                # we've figured out the real filename (hopefully!)
                setRealFileName(segment, foundFileName)
                
            onDiskSegments.append(segment)
        #else:
        #    debug('SKIPPING SEGMENT: ' + segment.getTempFileName() + ' subject: ' + \
        #          segment.nzbFile.subject)

    return needDlFiles, needDlSegments, onDiskSegments

class NZB:
    """ Representation of an nzb file -- the root <nzb> tag """
    nextId = 0
    
    def __init__(self, nzbFileName):
        ## NZB file general information
        self.nzbFileName = nzbFileName
        self.archiveName = archiveName(self.nzbFileName) # pretty name
        self.nzbFileElements = []
        
        self.id = self.getNextId()

        # Where the nzb files will be downloaded
        self.destDir = Hellanzb.WORKING_DIR

        ## A cancelled NZB is marked for death. ArticleDecoder will dispose of any
        ## recently downloaded data that might have been downloading during the time the
        ## cancel call was made (after the fact cleanup)
        self.canceled = False
        self.canceledLock = Lock()

        ## Whether or not we should redownload NZBFile and NZBSegment files on disk that are 0 bytes in
        ## size
        self.overwriteZeroByteFiles = True
        
    def getNextId(self):
        """ Return a new unique identifier """
        id = NZB.nextId
        NZB.nextId += 1
        return id

    def isCanceled(self):
        """ Whether or not this NZB was cancelled """
        self.canceledLock.acquire()
        c = self.canceled
        self.canceledLock.release()
        return c

    def cancel(self):
        """ Mark this NZB as having been cancelled """
        self.canceledLock.acquire()
        self.canceled = True
        self.canceledLock.release()
        
class NZBFile:
    """ <nzb><file/><nzb> """

    def __init__(self, subject, date = None, poster = None, nzb = None):
        ## XML attributes
        self.subject = subject
        self.date = date
        self.poster = poster

        ## XML tree-collections/references
        # Parent NZB
        self.nzb = nzb
        # FIXME: thread safety?
        self.nzb.nzbFileElements.append(self)
        
        self.groups = []
        self.nzbSegments = []

        ## TO download segments --
        # we'll remove from this set everytime a segment is found completed (on the FS)
        # during NZB parsing, or later written to the FS
        self.todoNzbSegments = Set()

        ## NZBFile statistics
        self.number = len(self.nzb.nzbFileElements)
        self.totalBytes = 0
        self.totalSkippedBytes = 0
        self.totalReadBytes = 0
        self.downloadPercentage = 0
        self.speed = 0
        self.downloadStartTime = None

        ## yEncode header keywords. Optional (not used for UUDecoded segments)
        # the expected file size, as reported from yencode headers
        self.ySize = None

        ## On Disk filenames
        # The real filename, determined from the actual articleData's yDecode/UUDecode
        # headers
        self.filename = None
        # The filename used temporarily until the real filename is determined
        self.tempFilename = None
        
        ## Optimizations
        # LAME: maintain a cached file name displayed in the scrolling UI, and whether or
        # not the cached name might be stale (might be stale = a temporary name). 
        self.showFilename = None
        self.showFilenameIsTemp = False
        
        # direct pointer to the first segment of this file, when we have a tempFilename we
        # look at this segment frequently until we find the real file name
        # FIXME: this most likely doesn't optimize for shit.
        self.firstSegment = None

        # LAME: re-entrant lock for maintaing temp filenames/renaming temp -> real file
        # names in separate threads. FIXME: This is a lot of RLock() construction, it
        # should be removed eventually
        self.tempFileNameLock = RLock() # this isn't used right
        # filename could be modified/accessed concurrently (getDestination called by the
        # downloader doesnt lock).
        # NOTE: maybe just change nzbFile.filename via the reactor (callFromThread), and
        # remove the lock entirely?

    def getDestination(self):
        """ Return the full pathname of where this NZBFile should be written to on disk """
        return self.nzb.destDir + os.sep + self.getFilename()

    def getFilename(self):
        """ Return the file name of where this NZBFile will lie on the filesystem (not including
        dirname). The filename information is grabbed from the first segment's articleData
        (uuencode's fault -- yencode includes the filename in every segment's
        articleData). In the case where a segment needs to know it's filename, and that
        first segment doesn't have articleData (hasn't been downloaded yet), a temp
        filename will be returned. Downloading segments out of order can easily occur in
        app like hellanzb that downloads the segments in parallel, thus the need for
        temporary file names """
        try:
            # FIXME: try = slow. just simply check if tempFilename exists after
            # getFilenamefromArticleData. does exactly the same thing w/ no try. should probably
            # looked at the 2nd revised version of this and make sure it's still as functional as
            # the original
            if self.filename != None:
                return self.filename
            elif self.tempFilename != None and self.firstSegment.articleData == None:
                return self.tempFilename
            else:
                # FIXME: i should only have to call this once after i get article
                # data. that is if it fails, it should set the real filename to the
                # incorrect tempfilename
                self.firstSegment.getFilenameFromArticleData()
                return self.tempFilename
        except AttributeError:
            self.tempFilename = self.getTempFileName()
            return self.tempFilename

    def needsDownload(self, workingDirListing = None):
        """ Whether or not this NZBFile needs to be downloaded (isn't on the file system). You may
        specify the optional workingDirListing so this function does not need to prune
        this directory listing every time it is called (i.e. prune directory
        names). workingDirListing should be a list of only filenames (basename, not
        including dirname) of files lying in Hellanzb.WORKING_DIR """
        start = time.time()
        # We need to ensure that we're not in the process of renaming from a temp file
        # name, so we have to lock.
        # FIXME: probably no longer True in any cases. These locks can probably be removed
    
        if workingDirListing == None:
            workingDirListing = []
            for file in os.listdir(Hellanzb.WORKING_DIR):
                if not validWorkingFile(Hellanzb.WORKING_DIR + os.sep + file,
                                        self.nzb.overwriteZeroByteFiles):
                    continue
                
                workingDirListing.append(file)
    
        if os.path.isfile(self.getDestination()):
            end = time.time() - start
            debug('needsDownload took: ' + str(end))
            return False
    
        elif self.filename == None:
            # We only know about the temp filename. In that case, fall back to matching
            # filenames in our subject line
            for file in workingDirListing:
                
                # Whole file match
                if self.subject.find(file) > -1:
                    end = time.time() - start
                    debug('needsDownload took: ' + str(end))
                    return False
    
        end = time.time() - start
        debug('needsDownload took: ' + str(end))
        return True

    def getTempFileName(self):
        """ Generate a temporary filename for this file, for when we don't have it's actual file
        name on hand """
        return 'hellanzb-tmp-' + self.nzb.archiveName + '.file' + str(self.number).zfill(4)

    def isAllSegmentsDecoded(self):
        """ Determine whether all these file's segments have been decoded """
        return not len(self.todoNzbSegments)

    #def __repr__(self):
    #    msg = 'nzbFile: ' + os.path.basename(self.getDestination())
    #    if self.filename != None:
    #        msg += ' tempFileName: ' + self.getTempFileName()
    #    msg += ' number: ' + str(self.number) + ' subject: ' + \
    #           self.subject
    #    return msg

class NZBSegment:
    """ <file><segment/></file> """
    
    def __init__(self, bytes, number, messageId, nzbFile):
        ## XML attributes
        self.bytes = bytes
        self.number = number
        self.messageId = messageId

        ## XML tree-collections/references
        # Reference to the parent NZBFile this segment belongs to
        self.nzbFile = nzbFile

        # This segment belongs to the parent nzbFile
        self.nzbFile.nzbSegments.append(self)
        self.nzbFile.todoNzbSegments.add(self)
        self.nzbFile.totalBytes += self.bytes

        ## Downloaded article data
        self.articleData = None

        ## yEncoder header keywords used for validation. Optional, obviously not used for
        ## UUDecoded segments
        self.yCrc = None # Not the original crc (upper()'d and lpadded with 0s)
        self.yBegin = None
        self.yEnd = None
        self.ySize = None

        ## A copy of the priority level of this segment, as set in the NZBQueue
        self.priority = None

        ## Any server pools that failed to download this file
        self.failedServerPools = []

    def getDestination(self):
        """ Where this decoded segment will reside on the fs """
        return self.nzbFile.getDestination() + '.segment' + str(self.number).zfill(4)
    
    def getTempFileName(self):
        """ """
        return self.nzbFile.getTempFileName() + '.segment' + str(self.number).zfill(4)

    def getFilenameFromArticleData(self):
        """ Determine the segment's filename via the articleData """
        parseArticleData(self, justExtractFilename = True)
        
        if self.nzbFile.filename == None and self.nzbFile.tempFilename == None:
            raise FatalError('Could not getFilenameFromArticleData, file:' + str(self.nzbFile) +
                             ' segment: ' + str(self))

    #def __repr__(self):
    #    return 'segment: ' + os.path.basename(self.getDestination()) + ' number: ' + \
    #           str(self.number) + ' subject: ' + self.nzbFile.subject

class RetryQueue:
    """ Maintains various PriorityQueues for requeued segments. Each PriorityQueue maintained
    is keyed by a string describing what serverPools previously failed to download that
    queue's segments """
    def __init__(self):
        # all the known pool names
        self.serverPoolNames = []
        
        # dict to lookup the priority by name -- the name describes which serverPools
        # should NOT look into that particular queue. Example: 'not1not2not4'
        self.poolQueues = {}

        # map of serverPoolNames to their list of valid retry queue names
        self.nameIndex = {}

        self.allNotNames = []

    def addServerPool(self, serverPoolName):
        self.serverPoolNames.append(serverPoolName)

    def removeServerPool(self, serverPoolName):
        # probably won't ever need this
        raise NotImplementedError()

    def requeueMissing(self, serverPoolName, segment):
        # determine where to put this from all it's failed server pools
        segment.failedServerPools.append(serverPoolName)
        
        notName = ''
        i = 0
        for poolName in self.serverPoolNames:
            i += 1
            if poolName not in segment.failedServerPools:
                continue
            notName += 'not' + str(i)

        if notName == '':
            raise PoolsExhausted
        ####debug('ADDING TO RETRY pool: ' + notName)
        self.poolQueues[notName].put((segment.priority, segment))
    
    def get(self, serverPoolName):
        valids = self.nameIndex[serverPoolName]
        for queueName in valids:
            queue = self.poolQueues[queueName]
            if len(queue):
                ####debug('((((((((((((((((((((((((((((((((((((((((((((((((((' + queueName)
                return queue.get_nowait()
        raise Empty()

    def createQueues(self):
        """ """
        for i in range(len(self.serverPoolNames)):
            notName = 'not' + str(i + 1)
            self.poolQueues[notName] = PriorityQueue()

            self._recurseCreateQueues([i], i, len(self.serverPoolNames))

        # Index every pool's list of valid retry queues they need to check
        i = 0
        for name in self.serverPoolNames:
            ####info('CREATED: ' + name)
            i += 1
            
            valids = []
            for notName in self.poolQueues.keys():
                if notName.find('not' + str(i)) > -1:
                    continue
                valids.append(notName)
            self.nameIndex[name] = valids

    def _recurseCreateQueues(self, currentList, currentIndex, totalCount):
        # Build the original notName
        notName = ''
        for i in currentList:
            notName += 'not' + str(i + 1)

        if len(currentList) >= totalCount - 1:
            # We've reached the end
            return

        for x in range(totalCount):
            if x == currentIndex or x in currentList:
                # We've already not'd x, skip it
                continue

            newList = currentList[:]
            newList.append(x)
            newList.sort()

            if newList in self.allNotNames:
                # this not name is equiv. to a not name we already generated. skip it
                continue

            self.allNotNames.append(newList)

            newNotName = notName + 'not' + str(x + 1)
            self.poolQueues[newNotName] = PriorityQueue()
            self._recurseCreateQueues(newList, x, totalCount)

class NZBQueue(PriorityQueue):
    """ priority fifo queue of segments to download. lower numbered segments are downloaded
    before higher ones """
    NZB_CONTENT_P = 100000 # normal nzb downloads
    EXTRA_PAR2_P = 0 # par2 after-the-fact downloads are more important

    def __init__(self, fileName = None):
        PriorityQueue.__init__(self)

        # Maintain a collection of the known nzbFiles belonging to the segments in this
        # queue. Set is much faster for _put & __contains__
        self.nzbFiles = Set()
        self.postponedNzbFiles = Set()
        self.nzbFilesLock = Lock()

        self.nzbs = []
        self.nzbsLock = Lock()

        self.totalQueuedBytes = 0

        self.retryQueueEnabled = False
        self.rQueue = RetryQueue()

        if fileName is not None:
            self.parseNZB(fileName)

    def cancel(self):
        self.postpone(cancel = True)

    def clear(self):
        PriorityQueue.clear(self)
        for queue in self.failedQueues.itervalues():
            queue.clear()

    def postpone(self, cancel = False):
        """ postpone the current download """
        self.clear()

        self.nzbsLock.acquire()
        self.nzbFilesLock.acquire()

        if not cancel:
            self.postponedNzbFiles.union_update(self.nzbFiles)
        self.nzbFiles.clear()

        self.nzbs = []
        
        self.nzbFilesLock.release()
        self.nzbsLock.release()

        self.totalQueuedBytes = 0

    def _put(self, item):
        """ Add a segment to the queue """
        priority, item = item

        # Support adding NZBFiles to the queue. Just adds all the NZBFile's NZBSegments
        if isinstance(item, NZBFile):
            offset = 0
            for nzbSegment in item.nzbSegments:
                PriorityQueue._put(self, (priority + offset, nzbSegment))
                offset += 1
        else:
            # Assume segment, add to list
            if item.nzbFile not in self.nzbFiles:
                self.nzbFiles.add(item.nzbFile)
            PriorityQueue._put(self, (priority, item))

    def calculateTotalQueuedBytes(self):
        """ Calculate how many bytes are queued to be downloaded in this queue """
        # NOTE: we don't maintain this calculation all the time, too much CPU work for
        # _put
        self.nzbFilesLock.acquire()
        files = self.nzbFiles.copy()
        self.nzbFilesLock.release()
        for nzbFile in files:
            self.totalQueuedBytes += nzbFile.totalBytes

    def currentNZBs(self):
        """ return a copy of the list of nzbs currently being downloaded """
        self.nzbsLock.acquire()
        nzbs = self.nzbs[:]
        self.nzbsLock.release()
        return nzbs

    def nzbAdd(self, nzb):
        """ denote this nzb as currently being downloaded """
        self.nzbsLock.acquire()
        self.nzbs.append(nzb)
        self.nzbsLock.release()
        
    def nzbDone(self, nzb):
        """ nzb finished """
        self.nzbsLock.acquire()
        try:
            self.nzbs.remove(nzb)
        except ValueError:
            # NZB might have been canceled
            pass
        self.nzbsLock.release()

    def serverAdd(self, serverPoolName):
        """ Let the queue know about the specified server pool. The queue will maintain sub-queues
        for each server pool. If a segment is missing from one server pool, hellanzb will
        attempt to download it on a different server pool. FIXME: GROUP docs """
        self.rQueue.addServerPool(serverPoolName)

    def initRetryQueue(self):
        self.retryQueueEnabled = True
        self.rQueue.createQueues()

    def serverRemove(self, serverPoolName):
        """ Remove the specified server pool """
        self.rQueue.removeServerPool(serverPoolName)
            
    def getSmart(self, serverPoolName):
        """ Get the next available segment in the queue. The 'smart'ness first checks for segments
        in the RetryQueue, otherwise it falls back to the main queue """
        # Don't bother w/ retryQueue nonsense unless it's enabled (meaning there are
        # multiple serverPools)
        if self.retryQueueEnabled:
            try:
                return self.rQueue.get(serverPoolName)
            except:
                # fall through
                pass
            
        return PriorityQueue.get_nowait(self)

    def requeueMissing(self, serverPoolName, segment):
        """ Requeue a missing segment. This segment will be added to the specified serverPool's
        failedQueue, where other serverPools will find it and reattempt the download """
        self.rQueue.requeueMissing(serverPoolName, segment)

    def fileDone(self, nzbFile):
        """ Notify the queue a file is done. This is called after assembling a file into it's
        final contents. Segments are really stored independantly of individual Files in
        the queue, hence this function """
        self.nzbFilesLock.acquire()
        if nzbFile in self.nzbFiles:
            self.nzbFiles.remove(nzbFile)
        self.nzbFilesLock.release()

    def segmentDone(self, nzbSegment):
        """ simply decrement the queued byte count, unless the segment is part of a postponed
        download """
        self.nzbsLock.acquire()
        if nzbSegment.nzbFile.nzb in self.nzbs:
            self.totalQueuedBytes -= nzbSegment.bytes
        self.nzbsLock.release()

    def parseNZB(self, nzb):
        """ Initialize the queue from the specified nzb file """
        # Create a parser
        parser = make_parser()
        
        # No XML namespaces here
        parser.setFeature(feature_namespaces, 0)
        parser.setFeature(feature_external_ges, 0)
        
        # Create the handler
        fileName = nzb.nzbFileName
        self.nzbAdd(nzb)
        needWorkFiles = []
        needWorkSegments = []
        dh = NZBParser(nzb, needWorkFiles, needWorkSegments)
        
        # Tell the parser to use it
        parser.setContentHandler(dh)

        # Parse the input
        try:
            parser.parse(fileName)
        except SAXParseException, saxpe:
            self.nzbDone(nzb)
            raise FatalError('Unable to parse Invalid NZB file: ' + os.path.basename(fileName))

        s = time.time()
        # The parser will add all the segments of all the NZBFiles that have not already
        # been downloaded. After the parsing, we'll check if each of those segments have
        # already been downloaded. it's faster to check all segments at one time
        needDlFiles, needDlSegments, onDiskSegments = segmentsNeedDownload(needWorkSegments,
                                                                           overwriteZeroByteSegments = \
                                                                           nzb.overwriteZeroByteFiles)
        e = time.time() - s

        onDiskCount = dh.fileCount - len(needWorkFiles)
        if onDiskCount:
            info('Parsed: ' + str(dh.segmentCount) + ' posts (' + str(dh.fileCount) + ' files, skipping ' + \
                 str(onDiskCount) + ' on disk files)')
        else:
            info('Parsed: ' + str(dh.segmentCount) + ' posts (' + str(dh.fileCount) + ' files)')

        # Tally what was skipped for correct percentages in the UI
        for nzbSegment in onDiskSegments:
            nzbSegment.nzbFile.totalSkippedBytes += nzbSegment.bytes

        # The needWorkFiles will tell us what nzbFiles are missing from the
        # FS. segmentsNeedDownload will further tell us what files need to be
        # downloaded. files missing from the FS (needWorkFiles) but not needing to be
        # downloaded (in needDlFiles) simply need to be assembled
        for nzbFile in needWorkFiles:
            if nzbFile not in needDlFiles:
                # Don't automatically 'finish' the NZB, we'll take care of that in this
                # function if necessary
                info(nzbFile.getFilename() + ': assembling -- all segments were on disk')
                
                # NOTE: this function is destructive to the passed in nzbFile! And is only
                # called on occasion (might bite you in the ass one day)
                try:
                    assembleNZBFile(nzbFile, autoFinish = False)
                except TooMuchWares:
                    self.nzbDone(nzb)
                    error('Cannot assemble ' + nzb.getFileName() + ': No space left on device! Exiting..')
                    shutdown(True)

        if not len(needDlSegments):
            # FIXME: this block of code is the end of tryFinishNZB. there should be a
            # separate function
            # nudge GC
            nzbFileName = nzb.nzbFileName
            self.nzbDone(nzb)
            info(nzb.archiveName + ': assembled archive!')
            for nzbFile in nzb.nzbFileElements:
                del nzbFile.todoNzbSegments
                del nzbFile.nzb
            del nzb.nzbFileElements
            # FIXME: put the above dels in NZB.__del__ (that's where collect can go if needed too)
            del nzb
            gc.collect()

            reactor.callLater(0, handleNZBDone, nzbFileName)
            # True == the archive is complete
            return True

        for nzbSegment in needDlSegments:
            self.put((nzbSegment.priority, nzbSegment))

        self.calculateTotalQueuedBytes()

        # Finally, figure out what on disk segments are part of partially downloaded
        # files. adjust the queued byte count to not include these aleady downloaded
        # segments. phew
        for nzbFile in needDlFiles:
            if len(nzbFile.todoNzbSegments) != len(nzbFile.nzbSegments):
                for segment in nzbFile.nzbSegments:
                    if segment not in nzbFile.todoNzbSegments:
                        self.segmentDone(segment)

        # Archive not complete
        return False

class NZBParser(ContentHandler):
    """ Parse an NZB 1.0 file into an NZBQueue
    http://www.newzbin.com/DTD/nzb/nzb-1.0.dtd """
    def __init__(self, nzb, needWorkFiles, needWorkSegments):
        # nzb file to parse
        self.nzb = nzb

        # to be populated with the files that either need to be downloaded or simply
        # assembled, and their segments
        self.needWorkFiles = needWorkFiles
        self.needWorkSegments = needWorkSegments

        # parsing variables
        self.file = None
        self.bytes = None
        self.number = None
        self.chars = None
        self.fileNeedsDownload = None
        
        self.fileCount = 0
        self.segmentCount = 0

        self.workingDirListing = []
        for file in os.listdir(Hellanzb.WORKING_DIR):
            if not validWorkingFile(Hellanzb.WORKING_DIR + os.sep + file,
                                    self.nzb.overwriteZeroByteFiles):
                continue

            self.workingDirListing.append(file)

    def startElement(self, name, attrs):
        if name == 'file':
            subject = self.parseUnicode(attrs.get('subject'))
            poster = self.parseUnicode(attrs.get('poster'))

            self.file = NZBFile(subject, attrs.get('date'), poster, self.nzb)
            self.fileNeedsDownload = self.file.needsDownload(workingDirListing = self.workingDirListing)
            if not self.fileNeedsDownload:
                debug('SKIPPING FILE: ' + self.file.getTempFileName() + ' subject: ' + \
                      self.file.subject)

            self.fileCount += 1
            self.file.number = self.fileCount
                
        elif name == 'group':
            self.chars = []
                        
        elif name == 'segment':
            self.bytes = int(attrs.get('bytes'))
            self.number = int(attrs.get('number'))
                        
            self.chars = []
        
    def characters(self, content):
        if self.chars is not None:
            self.chars.append(content)
        
    def endElement(self, name):
        if name == 'file':
            if self.fileNeedsDownload:
                self.needWorkFiles.append(self.file)
            else:
                # done adding all child segments to this NZBFile. make note that none of
                # them need to be downloaded
                self.file.todoNzbSegments.clear()

                # FIXME: (GC) can we del self.nzbfile here???
            
            self.file = None
            self.fileNeedsDownload = None
                
        elif name == 'group':
            newsgroup = self.parseUnicode(''.join(self.chars))
            self.file.groups.append(newsgroup)
                        
            self.chars = None
                
        elif name == 'segment':
            self.segmentCount += 1

            messageId = self.parseUnicode(''.join(self.chars))
            nzbs = NZBSegment(self.bytes, self.number, messageId, self.file)
            if self.segmentCount == 1:
                self.file.firstSegment = nzbs

            if self.fileNeedsDownload:
                # HACK: Maintain the order in which we encountered the segments by adding
                # segmentCount to the priority. lame afterthought -- after realizing
                # heapqs aren't ordered. NZB_CONTENT_P must now be large enough so that it
                # won't ever clash with EXTRA_PAR2_P + i
                nzbs.priority = NZBQueue.NZB_CONTENT_P + self.segmentCount
                self.needWorkSegments.append(nzbs)

            self.chars = None
            self.number = None
            self.bytes = None    

    def parseUnicode(self, unicodeOrStr):
        if isinstance(unicodeOrStr, unicode):
            return unicodeOrStr.encode('latin-1')
        return unicodeOrStr
        
"""
/*
 * Copyright (c) 2005 Philip Jenvey <pjenvey@groovie.org>
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 * 3. The name of the author or contributors may not be used to endorse or
 *    promote products derived from this software without specific prior
 *    written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE.
 *
 * $Id$
 */
"""
