"""

Core - All of our main()ish functions. Initialization/shutdown/etc

(c) Copyright 2005 Philip Jenvey, Ben Bangert
[See end of file]
"""
# Install our custom twisted reactor immediately
from Hellanzb.HellaReactor import HellaReactor
HellaReactor.install()

import optparse, os, signal, sys, time, thread, threading, Hellanzb, Hellanzb.PostProcessor
from distutils import spawn
from shutil import rmtree
from threading import Lock
from twisted.internet import reactor
from Hellanzb.Daemon import initDaemon, postProcess
from Hellanzb.HellaXMLRPC import hellaRemote, initXMLRPCClient
from Hellanzb.Log import *
from Hellanzb.Logging import initLogging, stdinEchoOn
from Hellanzb.PostProcessorUtil import defineMusicType
from Hellanzb.Util import *

__id__ = '$Id$'

def findAndLoadConfig(optionalConfigFile = None):
    """ Find and load the configuration file """
    if optionalConfigFile != None:
        if loadConfig(optionalConfigFile):
            return
        else:
            error('Unable to load specified config file: ' + optionalConfigFile)
            sys.exit(1)

    # look for conf in this order: sys.prefix, ./, or ./etc/
    confDirs = [ sys.prefix + os.sep + 'etc', os.getcwd() + os.sep + 'etc', os.getcwd() ]

    # hard coding preferred Darwin config file location, kind of lame. but I'd rather do
    # this then make an etc dir in os x's Python.framework directory
    (sysname, nodename, release, version, machine) = os.uname()
    if sysname == "Darwin":
        confDirs[0] = '/opt/local/etc'

    foundConfig = False
    for dir in confDirs:
        file = dir + os.sep + 'hellanzb.conf'
        
        if loadConfig(file):
            return
        
    error('Could not find configuration file in the following dirs: ' + str(confDirs))
    sys.exit(1)
    
def loadConfig(fileName):
    """ Attempt to load the specified config file. If successful, clean the variables/data the
    config file has setup """
    if not os.path.isfile(fileName):
        return False

    if not os.access(fileName, os.R_OK):
        warn('Unable to read config file: ' + fileName)
        return False

    try:        
        execfile(fileName)
        
        # Cache this operation (whether or not we're in debug mode) for faster (hardly)
        # debug spamming (from NZBLeecher)
        Hellanzb.DEBUG_MODE_ENABLED = False
        if hasattr(Hellanzb, 'DEBUG_MODE') and Hellanzb.DEBUG_MODE != None and \
                Hellanzb.DEBUG_MODE != False:
            # Set this ASAP for sane logging. FIXME: You could possibly lose some debug
            # output during initialization if you're using the -d option
            Hellanzb.DEBUG_MODE_ENABLED = True

        # Ensure the types are lower case
        for varName in ('NOT_REQUIRED_FILE_TYPES', 'KEEP_FILE_TYPES'):
            types = getattr(Hellanzb, varName)
            lowerTypes = [ext.lower() for ext in types]
            setattr(Hellanzb, varName, lowerTypes)

        if not hasattr(Hellanzb, 'MAX_RATE') or Hellanzb.MAX_RATE == None:
            Hellanzb.MAX_RATE = 0
        else:
            Hellanzb.MAX_RATE = int(Hellanzb.MAX_RATE)

        if not hasattr(Hellanzb, 'SKIP_UNRAR') or Hellanzb.SKIP_UNRAR == None:
            Hellanzb.SKIP_UNRAR = False
            
        debug('Found config file in directory: ' + os.path.dirname(fileName))
        return True
    
    except FatalError, fe:
        error('A problem occurred while reading the config file', fe)
        raise
    except Exception, e:
        msg = 'An unexpected error occurred while reading the config file'
        error(msg, e)
        raise

# FIXME I think due to the recent change that shutdown()s, then logs -- logShutdown can be
# replaced with normal logging calls
def signalHandler(signum, frame):
    """ The main and only signal handler. Handle cleanup/managing child processes before
    exiting """
    # CTRL-C
    if signum == signal.SIGINT:
        # If there aren't any proceses to wait for exit immediately
        if len(Topen.activePool) == 0:
            shutdown()
            logShutdown('Caught interrupt, exiting..')
            return

        # We can safely exit ASAP if all the processes are associated with the main thread
        # (the thread processes? seem to have have already gotten the signal as well at
        # this point. I'm not exactly sure why)
        threadsOutsideMain = False
        for topen in Topen.activePool:
            if topen.threadIdent != Hellanzb.MAIN_THREAD_IDENT:
                threadsOutsideMain = True

        if not threadsOutsideMain:
            shutdown()
            logShutdown('Caught interrupt, exiting..')
            return

        # We couldn't cheat our way out of the program, tell the user the processes
        # (threads) we're waiting on, and wait for another signal
        if Hellanzb.stopSignalCount == 0 or (time.time() - Hellanzb.firstSignal > 5):
            Hellanzb.firstSignal = time.time()
            Hellanzb.stopSignalCount = 1
        else:
            Hellanzb.stopSignalCount = Hellanzb.stopSignalCount + 1

        if Hellanzb.stopSignalCount < 2:
            msg = 'Caught interrupt, waiting for these child processes to finish:\n'
            for topen in Topen.activePool:
                msg += truncateToMultiLine(topen.prettyCmd, length = 68,
                                           prefix = str(topen.getPid()) + '  ',
                                           indentPrefix = ' '*8) + '\n'
            msg += '(CTRL-C again within 5 seconds to kill them and exit immediately.\n' + \
                'PostProcessors will automatically resume when hellanzb is restarted)'
            warn(msg)
            
        else:
            # Kill the processes. If any processes are lying around after a kill -9, it's
            # either an o/s problem (we don't care) or a bug in hellanzb (we aren't
            # allowing the process to exit/still reading from it)
            warn('Killing child processes..')
            Topen.killAll()
            shutdown()
            logShutdown('Killed all child processes, exiting..')
            return
            
def assertHasARar():
    """ assertIsExe rar or its doppelganger """
    Hellanzb.UNRAR_CMD = None
    for exe in [ 'rar', 'unrar' ]:
        if spawn.find_executable(exe):
            Hellanzb.UNRAR_CMD = exe
    if not Hellanzb.UNRAR_CMD:
        err = 'Cannot continue program, required executable \'rar\' or \'unrar\' not in path'
        raise FatalError(err)
    assertIsExe(Hellanzb.UNRAR_CMD)

def init(options = {}):
    """ initialize the app """
    # Whether or not the app is in the process of shutting down
    Hellanzb.SHUTDOWN = False

    # Get logging going ASAP
    initLogging()

    # CTRL-C shutdown return code
    Hellanzb.SHUTDOWN_CODE = 20

    # defineServer's from the config file
    Hellanzb.SERVERS = {}

    # we can compare the current thread's ident to our MAIN_THREAD's to determine whether
    # or not we may need to route things through twisted's callFromThread
    Hellanzb.MAIN_THREAD_IDENT = thread.get_ident()

    Hellanzb.BEGIN_TIME = time.time()

    # Whether or not the downloader has been paused
    Hellanzb.downloadPaused = False

    # Troll threads
    Hellanzb.postProcessors = []
    Hellanzb.postProcessorLock = Lock()

    # How many total NZB archives have been post processed
    Hellanzb.totalPostProcessed = 0

    # Whether or not the queue daemon is running as a daemon process (forked)
    Hellanzb.DAEMONIZE = False

    # How many times CTRL-C has been pressed
    Hellanzb.stopSignalCount = 0
    # When the first CTRL-C was pressed
    Hellanzb.firstSignal = None

    # Whether or not the C yenc module is installed
    try:
        import _yenc
        Hellanzb.HAVE_C_YENC = True
    except ImportError:
        Hellanzb.HAVE_C_YENC = False

    assertHasARar()

    # Twisted will replace this with its own signal handler when initialized
    signal.signal(signal.SIGINT, signalHandler)

    outlineRequiredDirs() # before the config file is loaded
        
    if hasattr(options, 'configFile') and options.configFile != None:
        findAndLoadConfig(options.configFile)
    else:
        findAndLoadConfig()

    # FIXME: these blocks below, and some code in loadConfig should all be pulled out into
    # a post-loadConfig normalizeConfig function
    for attr in ('logFile', 'debugLogFile'):
        # this is really: logFile = None
        setattr(sys.modules[__name__], attr, None)
        if hasattr(options, attr) and getattr(options, attr) != None:
            setattr(sys.modules[__name__], attr, getattr(options, attr))
    Hellanzb.Logging.initLogFile(logFile = logFile, debugLogFile = debugLogFile)

    # overwrite xml rpc vars from the command line options if they were set
    for option, attr in { 'rpcServer': 'XMLRPC_SERVER',
                          'rpcPassword': 'XMLRPC_PASSWORD',
                          'rpcPort': 'XMLRPC_PORT' }.iteritems():
        if hasattr(options, option) and getattr(options, option) != None:
            setattr(Hellanzb, attr, getattr(options, option))

    if not hasattr(Hellanzb, 'GROWL_NOTIFY'):
        error('Required option not defined in config file: Hellanzb.GROWL_NOTIFY')
        sys.exit(1)
    elif Hellanzb.GROWL_NOTIFY:
        errors = []
        for attr in ('GROWL_SERVER', 'GROWL_PASSWORD'):
            if not hasattr(Hellanzb, attr):
                err = 'Hellanzb.GROWL_NOTIFY enabled. Required option not defined in config file: Hellanzb.'
                errors.append(err + attr)
        if len(errors):
            [error(err) for err in errors]
            sys.exit(1)

def outlineRequiredDirs():
    """ Set all required directory attrs to None. they will be checked later for this value to
    ensure they have been set """
    requiredDirs = [ 'PREFIX', 'QUEUE', 'DEST', 'CURRENT', 'WORKING',
                     'POSTPONED', 'PROCESSING', 'TEMP' ]
    for dir in requiredDirs:
        setattr(Hellanzb, dir + '_DIR', None)

def shutdown(killPostProcessors = False):
    """ Turn the knob that tells all parts of the program we're shutting down, optionally kill
    any sub processes (that could prevent the program from exiting) and kill the twisted
    reactor """
    # that knob, that threads (PostProcessors) will check on before doing significant work
    Hellanzb.SHUTDOWN = True

    if killPostProcessors:
        # However PostProcessors may be running sub-processes, which are all kill -9ed
        # here
        Topen.killAll()

    # stop the twisted reactor
    reactor.callLater(0, reactor.stop)

    # Just in case we left it off
    stdinEchoOn()

    if hasattr(Hellanzb, 'DOWNLOAD_TEMP_DIR'):
        # Remove the temporary files with the encoded data. Any errors causing hellanzb to
        # shut down prematurely (like can't bind to specific port -- maybe another
        # hellanzb is running?) should unset this var so this doesn't get called
        rmtree(Hellanzb.DOWNLOAD_TEMP_DIR)
    
def shutdownAndExit(returnCode = 0):
    """ Shutdown hellanzb's twisted reactor, AND call sys.exit """
    shutdown()

    sys.exit(returnCode)

def marquee():
    """ Print a simple header, for when starting the app """
    info('')
    msg = 'hellanzb v' + Hellanzb.version

    options = []
    if Hellanzb.DAEMONIZE:
        options.append('daemonized')
    if Hellanzb.HAVE_C_YENC:
        options.append('C yenc module')

    optionLen = len(options)
    if optionLen:
        msg += ' ('

        i = 0
        for option in options:
            msg += option
            i += 1
            if i < optionLen:
                msg += ', '
        msg += ')'
        
    info(msg)
    debug(msg)

USAGE = """
hellanzb version %s
""".lstrip() + cmHella().rstrip() + \
"""
   nzb downloader and post processor
   http://www.hellanzb.com

usage: %s [options] [remote-call] [remote-call-options]

hellanzb will by default (no remote-call specified) start its one and only
queue daemon. Specifying a remote call will attempt to talk to that already
running queue daemon via XML-RPC.

remote-calls (via XML-RPC):
%s
""".rstrip()
def parseArgs():
    """ Parse the command line args """
    # prevent optparse from totally munging usage
    formatter = optparse.IndentedHelpFormatter()
    formatter.format_usage = lambda usage: usage

    # Initialize this here, so we can probe it for xml rpc client commands in the usage
    initXMLRPCClient()
    from Hellanzb.HellaXMLRPC import RemoteCall
    usage = USAGE % (str(Hellanzb.version), '%prog', RemoteCall.allUsage())
    
    parser = optparse.OptionParser(formatter = formatter, usage = usage, version = Hellanzb.version)
    parser.add_option('-c', '--config', type='string', dest='configFile',
                      help='specify the configuration file')
    parser.add_option('-l', '--log-file', type='string', dest='logFile',
                      help='specify the log file (overwrites the Hellanzb.LOG_FILE config file setting)')
    parser.add_option('-d', '--debug-file', type='string', dest='debugLogFile',
                      help='specify the debug log file (turns on debugging output/overwrites the ' + \
                      'Hellanzb.DEBUG_MODE config file setting)')
    parser.add_option('-D', '--daemon', action='store_true', dest='daemonize',
                      help='run hellanzb as a daemon process (fork and exit)')
    #parser.add_option('-n', '--just-download-nzb', type='string', dest='justDownload',
    #                  help='download the specified nzb and exit the program (do not post process)')
    parser.add_option('-p', '--post-process-dir', type='string', dest='postProcessDir',
                      help='post-process the specified nzb archive dir either in an already running hellanzb' + \
                      ' (via xmlrpc) if one is available, otherwise in the current process. then exit')
    parser.add_option('-P', '--rar-password', type='string', dest='rarPassword',
                      help='when used with the -p option, specifies the nzb archive\'s rar password')
    parser.add_option('-L', '--local-post-process', action='store_true', dest='localPostProcess',
                      help='when used with the -p option, do the post processing work in the current ' + \
                      'process (do not attempt to contact an already running queue daemon)')
    parser.add_option('-r', '--rpc-server', type='string', dest='rpcServer',
                      help='specify the rpc server hostname (overwrites Hellanzb.XMLRPC_SERVER config file setting)')
    parser.add_option('-s', '--rpc-password', type='string', dest='rpcPassword',
                      help='specify the rpc server password (overwrites Hellanzb.XMLRPC_PASSWORD config file setting)')
    parser.add_option('-t', '--rpc-port', type='int', dest='rpcPort',
                      help='specify the rpc server port (overwrites Hellanzb.XMLRPC_PORT config file setting)')
    return parser.parse_args()

def processArgs(options, args):
    """ By default (no args) run the daemon. Otherwise we could be making an XML RPC call, or
    calling a PostProcessor on the specified dir then exiting """
    if not len(args) and not options.postProcessDir:

        if options.daemonize:
            # Run as a daemon process (fork)
            Hellanzb.DAEMONIZE = True

        marquee()
        initDaemon()

    elif options.postProcessDir and options.localPostProcess:
        marquee()
        reactor.callLater(0, postProcess, options)
        reactor.run()

    else:
        try:
            hellaRemote(options, args)
        except SystemExit, se:
            # sys.exit throws this, let it go
            raise
        except FatalError, fe:
            error('Exiting', fe)
            shutdownAndExit(1)
        except Exception, e:
            error('An unexpected problem occurred, exiting', e)
            shutdown()
            raise

def main():
    """ Program main loop. Always called from the main thread """
    options, args = parseArgs()

    try:
        init(options)
    
    except SystemExit, se:
        # sys.exit throws this, let it go
        raise
    except FatalError, fe:
        error('Exiting', fe)
        shutdownAndExit(1)
    except Exception, e:
        error('An unexpected problem occurred, exiting', e)
        shutdown()
        raise

    processArgs(options, args)

"""
/*
 * Copyright (c) 2005 Philip Jenvey <pjenvey@groovie.org>
 *                    Ben Bangert <bbangert@groovie.org>
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
