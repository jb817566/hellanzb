# ---------------------------------------------------------------------------
# $Id: HeadHoncho.py,v 1.104 2004/10/09 08:27:01 freddie Exp $
# ---------------------------------------------------------------------------
# Main class used to control everything

import binascii
import bisect
import nntplib
import os
import re
import select
import socket
import sys
import time

import Hellanzb

from zlib import crc32

from NZBParser import ParseNZB
from WrapPost import WrapPost
from WrapServer import WrapServer

# We need the useful yenc module
sys.path.append(os.path.expanduser('~/lib/python'))
try:
	import _yenc
	HAVE_YENC = 1
except ImportError:
	HAVE_YENC = 0

# ---------------------------------------------------------------------------

CHECK_FAIL = 0
CHECK_PASS = 1
CHECK_NEED_MORE = 2
CHECK_IGNORE = 3

TYPE_UUENCODE = 'uuencode'
TYPE_YENC = 'yenc'

# ---------------------------------------------------------------------------

def ShowError(text, *args):
	if args:
		text = text % args
	print 'ERROR: %s' % text
	sys.exit(-1)

class HeadHoncho:
	def __init__(self, jobs):
		self.jobs = jobs
		
		# Set up our connections
		self.FDs = {}
		self.Servers = []
		
		# Groups we've updated already
		self.Updated = {}

		self._incomplete_threshold = Hellanzb.Newsleecher.INCOMPLETE_THRESHOLD
		
		# Work out our servers. First we sort by priority, then name.
		servers = []
		
                for id in Hellanzb.SERVERS:
                        serverInfo = Hellanzb.SERVERS[id]
                        # We're keying by the id here, store it for later use
                        serverInfo['id'] = id

                        priority = serverInfo['priority']
                        servers.append((priority, serverInfo))
                        
		servers.sort()
		
		# Now actually start them up
		for priority, serverInfo in servers:
			# Build our wrap
			swrap = WrapServer(serverInfo)
			self.Servers.append(swrap)
			
			# Connect
			swrap.connect()
			if swrap.Conns:
				for fd in swrap.Conns.keys():
					self.FDs[fd] = swrap
			else:
				self.Servers.pop()
		
		# Did we connect?
		if not self.FDs:
			ShowError('failed to open any server connections!')
	
	# ---------------------------------------------------------------------------
	# Our main loop, obviously
	def main_loop(self):
		for job in self.jobs:
			print
			
			# Is it a nzb job?
			if job.endswith('.nzb'):
				self.nzb_job(job)

			# Wtf is it then?
			else:
				ShowError("unknown job type '%s'", job)
	
	# ---------------------------------------------------------------------------
	# Does all the stuff we need for a 'nzb' job.
	def nzb_job(self, job):
		# Make sure the file exists first
		if not os.access(job, os.R_OK):
			print "File '%s' does not exist or is not readable!"
			return
		
		print "Attempting to parse '%s'..." % job,
		sys.stdout.flush()
		
		try:
			newsgroups, posts = ParseNZB(job, self.Servers)
		
		except Exception, msg:
			print 'failed: %s' % msg
			return
		
		print 'found %d posts.' % (len(posts))
		
		
		if posts:
			useful = 0
			
			for swrap in self.Servers:
				found = 0
				
				for newsgroup in newsgroups:
					groupdata = swrap.set_group(newsgroup)
					if groupdata is not None:
						found = 1
						useful += 1
						break
				
				if not found:
					print '(%s) No valid groups found!' % (swrap.name)
			
			if useful:
				self.get_bodies(posts)

	# ---------------------------------------------------------------------------
	# Retrieve a set of bodies with multiple connections, arggh
	def get_bodies(self, posts):
		# Function shortcuts
		_select = select.select
		_sleep = time.sleep
		_time = time.time
		
		# Initialise some variables
		active = []
		ready = []
		
		for swrap in self.Servers:
			for fd, nwrap in swrap.Conns.items():
				nwrap.setblocking(0)
				ready.append(fd)
		
		leech_start = _time()
		leech_raw_bytes = 0
		leech_files = 0
		
		# Off we go
		subjects = posts.keys()
		subjects.sort(magic_sort)
		
		for subject in subjects:
			pwrap = posts[subject]
			
			haveparts = len(pwrap.parts)
			
			# If we don't have enough parts to bother with, skip it
			if (float(haveparts) / pwrap.numparts * 100) < self._incomplete_threshold:
				print "* '%s' is less than %d%% complete, skipping" % (subject, self._incomplete_threshold)
				continue
			
			# Some more variables
			datafile = None
			filename = None
			nextpart = 1
			percent = -1
			skipfile = 0
			speed = 0.0
			status = 0
			
			file_dec_bytes = 0
			file_raw_bytes = 0
			file_start = _time()
			
			file_parts = []
			file_type = None
			
			# Grab the bits!
			while 1:
				if ready:
					# If there are more posts, ask for the next one
					if posts[subject].parts and not skipfile:
						if status != 1:
							# Get the next part
							partnum, part = posts[subject].get_next_part()
							
							# Find a server connection for it
							for swrap in part[1:]:
								fds = [fd for fd in ready if self.FDs[fd] == swrap]
								if fds:
									del posts[subject].parts[partnum]
									
									i = ready.index(fds[0])
									fd = ready.pop(i)
									active.append(fd)
									
									#print '(%s) Retrieving part %d: %s' % (swrap.name, partnum, part[0])
									
									nwrap = swrap.Conns[fd]
									
									nwrap._msgid = part[0]
									nwrap._partnum = partnum
									
									nwrap.body(part[0])
									
									if status == 0:
										status = 1
									
									break
					
					# If we're all done, run away
					elif not active:
						break
				
				# Select!
				can_read = _select(active, [], [], 0)[0]
				if can_read:
					currtime = _time()
					
					for fd in can_read:
						nwrap = self.FDs[fd].Conns[fd]
						read_bytes, done = nwrap.recv_chunk()
						
						# Update counters
						file_raw_bytes += read_bytes
						leech_raw_bytes += read_bytes
						
						# If we're skipping this file, just do that
						if skipfile:
							if done:
								nwrap.reset()
								active.remove(fd)
								ready.append(fd)
							continue
						
						# If we just have the one line, it may be an error
						if len(nwrap.lines) == 1:
							# No Such Article In Group
							if nwrap.lines[0][:3] in ('423', '430'):
								print '* Article is missing!'
								
								active.remove(fd)
								ready.append(fd)
								nwrap.reset()
								
								if status == 1:
									status = 0
								
								# Add some dummy data
								bisect.insort_left(file_parts, (nwrap._partnum, ''))
								
								continue
							
							# 2xx is OK. I think.
							elif not nwrap.lines[0].startswith('2'):
								print nwrap.lines[0]
						
						# If we're checking this post, do that now
						if status == 1 and not skipfile and len(nwrap.lines) >= 1:
							result = Check_Post(nwrap, 1)
							
							if result[0] == CHECK_NEED_MORE:
								continue
							
							elif result[0] == CHECK_FAIL:
								skipfile = 1
							
							elif result[0] == CHECK_PASS:
								ybegin = result[1]
								
								# If it's yEnc, make sure it's not complete already
								if ybegin:
									filename = ybegin['name']
									if os.path.isfile(filename):
										currsize = os.path.getsize(filename)
										if currsize == int(ybegin['size']):
											print '\r* Skipping %s, already complete  ' % filename
											
											# Skip it
											skipfile = 1
									
									if not skipfile:
										file_type = TYPE_YENC
								
								# It's uu, blarg
								else:
									file_type = TYPE_UUENCODE
									filename = nwrap.lines[1].split(None, 2)[2]
								
								# Open the data file now if we have to
								if not skipfile:
									datafile = open(filename, 'wb')
									leech_files += 1
							
							# If we have to skip it, do that
							if skipfile:
								if done:
									nwrap.reset()
									active.remove(fd)
									ready.append(fd)
								status = 0
								continue
							
							# We're ok then
							status = 2
						
						
						# See if we have to update the percentage display 
						if not skipfile:
							newper = min(100, int(float(file_raw_bytes) / max(1, posts[subject].totalbytes) * 100))
							
							if newper > percent:
								percent = newper
								
								elapsed = max(0.1, currtime - file_start)
								
								speed = file_raw_bytes / elapsed / 1024.0
								#eta = Nice_Time((size - got - resumed) / 1024.0 / speed)
								
								print '\r* Decoding %s - %2d%% @ %.1fKB/s' % (filename, percent, speed),
								sys.stdout.flush()
						
						# Still more to do
						if not done:
							continue
						
						# Put it back in the ready queue
						active.remove(fd)
						ready.append(fd)
						
						# Make sure the file is usable
						result = Check_Post(nwrap, 0, file_type)
						
						if result[0] == CHECK_NEED_MORE:
							print 'CHECK_NEED_MORE? no way.'
							continue
						
						elif result[0] == CHECK_FAIL:
							skipfile = 1
							continue
						
						elif result[0] == CHECK_PASS:
							ybegin = result[1]
						
						elif result[0] == CHECK_IGNORE:
							print '* Ignoring stupid article.'
							continue
						
						
						# yEnc format
						if file_type == TYPE_YENC:
							# look for the =yend line
							_partcrc = ''
							numlines = len(nwrap.lines)
							for i in range(numlines - 1, numlines - 20, -1):
								if nwrap.lines[i].startswith('=yend'):
									yend = ySplit(nwrap.lines[i])
									if ('pcrc32' in yend):
										_partcrc = '0' * (8 - len(yend['pcrc32'])) + yend['pcrc32'].upper()
									elif ('crc32' in yend and yend.get('part', '1') == '1'):
										_partcrc = '0' * (8 - len(yend['crc32'])) + yend['crc32'].upper()
									else:
										print '* Invalid =yend line!'
										print '==> %s' % repr(nwrap.lines[i])
										sys.exit(1)
									
									# if there's a =ypart line, skip the first two
									if nwrap.lines[1].startswith('=ypart'):
										ypart = ySplit(nwrap.lines[1])
										nwrap.lines = nwrap.lines[2:i]
									else:
										ypart = {}
										nwrap.lines = nwrap.lines[1:i]
									
									# un-double-dot any lines :\
									for i in xrange(len(nwrap.lines)):
										if nwrap.lines[i][:2] == '..':
											nwrap.lines[i] = nwrap.lines[i][1:]
									
									break
							
							# If we found no pcrc32, run away
							if not _partcrc:
								print '\r* No valid =yend line found in part %d, skipping!' % (nwrap._partnum)
								nwrap.reset()
								continue
							
							# Decode the data and check the crc32
							if HAVE_YENC:
								decoded, tempcrc = _yenc.decode_string(''.join(nwrap.lines))[:2]
								partcrc = '%08X' % ((tempcrc ^ -1) & 2**32L - 1)
							else:
								decoded = yDecode(''.join(nwrap.lines))
								partcrc = '%08X' % (crc32(decoded) & 2**32L - 1)
							
							if partcrc != _partcrc:
								print '\n* CRC mismatch in part %d: %s != %s' % (partnum, partcrc, _partcrc)
							
							# Keep it around for a bit
							bisect.insort_left(file_parts, (nwrap._partnum, decoded))
						
						
						# uuencode format
						elif file_type == TYPE_UUENCODE:
							# Eat any trailing empty lines
							while nwrap.lines[-1] == '':
								nwrap.lines.pop(-1)
							
							# If this is the first part, eat the begin line
							if nwrap._partnum == 1:
								start = 1
							else:
								start = 0
							
							# If this is the end, eat the last bits too
							if nwrap.lines[-1] == 'end' and nwrap.lines[-2] == '`':
								end = len(nwrap.lines) - 2
							else:
								end = len(nwrap.lines)
							
							# Decode it
							chunks = []
							
							for i in range(start, end):
								try:
									data = binascii.a2b_uu(nwrap.lines[i])
									chunks.append(data)
								except binascii.Error, msg:
									# Workaround for broken uuencoders by /Fredrik Lundh
									nbytes = (((ord(nwrap.lines[i][0])-32) & 63) * 4 + 5) / 3
									try:
										data = binascii.a2b_uu(nwrap.lines[i][:nbytes])
										chunks.append(data)
									except binascii.Error, msg:
										print '\n* Decode failed in part %d: %s' % (nwrap._partnum, msg)
										print '=> %s' % (repr(nwrap.lines[i]))
							
							bisect.insort_left(file_parts, (nwrap._partnum, ''.join(chunks)))
						
						
						# Maybe save it now
						while file_parts and file_parts[0][0] == nextpart:
							decoded = file_parts.pop(0)[1]
							
							datafile.write(decoded)
							
							file_dec_bytes += len(decoded)
							nextpart += 1
						
						
						# Reset the connection to a useful state
						nwrap.reset()
				
				
				# Sleep for a little bit
				_sleep(0.01)
			
			
			# Done with this file
			if datafile is not None:
				# Write any leftover parts properly
				while file_parts:
					decoded = file_parts.pop(0)[1]
					datafile.write(decoded)
					file_dec_bytes += len(decoded)
				
				datafile.close()
				datafile = None
			
			# Spit out some nice info
			if filename and not skipfile:
				dur = time.time() - file_start
				speed = file_raw_bytes / 1024.0 / dur
				
				fdb = NiceSize(file_dec_bytes)
				
				print '\r* Decoded %s (%s) in %.1fs at %.1fKB/s' % (filename, fdb, dur, speed)
				
				if file_type == TYPE_YENC and file_dec_bytes != int(ybegin['size']):
					print '** File is incomplete!'
		
		
		# Spit out some nice info
		if leech_raw_bytes:
			dur = time.time() - leech_start
			speed = leech_raw_bytes / 1024.0 / dur
			ldb = NiceSize(leech_raw_bytes)
			
			print 'Transferred %s in %.1fs at %.1fKB/s' % (ldb, dur, speed)
		
		
		# Clean up
		for swrap in self.Servers:
			for fd, nwrap in swrap.Conns.items():
				nwrap.setblocking(1)

# ---------------------------------------------------------------------------
# Check a post to make sure it's usable.
def Check_Post(nwrap, line, file_type=None):
	# Eat leading whitespace lines
	for i in range(line, len(nwrap.lines)):
		if nwrap.lines[line] == '':
			nwrap.lines.pop(line)
	
	# Oops, we ate everything.
	if len(nwrap.lines[line:]) == 0:
		return (CHECK_NEED_MORE, None)
	
	
	# Check the first ten lines for post stuff
	for l in range(line, min(len(nwrap.lines), line+10)):
		# Check for uuencode message
		if nwrap.lines[l].startswith('begin '):
			if l > line:
				nwrap.lines = nwrap.lines[l:]
			return (CHECK_PASS, None)
		
		# Check for yEnc message
		elif nwrap.lines[l].startswith('=ybegin'):
			# See if we can parse the =ybegin line
			ybegin = ySplit(nwrap.lines[l])
			if not ('line' in ybegin and 'size' in ybegin and 'name' in ybegin):
				print '* Invalid =ybegin line in part %d!' % (nwrap.partnum)
				print '==> %s' % repr(nwrap.lines[l])
				
				return (CHECK_FAIL,)
			
			# Seems to be OK
			if l > line:
				nwrap.lines = nwrap.lines[l:]
			return (CHECK_PASS, ybegin)
		
		# Check for goddamn ads
		if nwrap.lines[l].find('Posted via Newsfeed') >= 0:
			return (CHECK_IGNORE, None)
	
	# Probably uu, keep going
	if file_type == TYPE_UUENCODE:
		return (CHECK_PASS, None)
	
	# No idea
	else:
		print '\r* Not uuencode or yEnc format post, skipping!'
		for line in nwrap.lines[line:line+5]:
			print '==> %s' % repr(line)
		
		return (CHECK_FAIL, None)

# ---------------------------------------------------------------------------

def magic_sort(a, b):
	'Try to sort NFOs and SFVs first'
	la = a.lower()
	lb = b.lower()
	
	na = la.find('.nfo')
	nb = lb.find('.nfo')
	
	if na >= 0 and nb < 0:
		return -1
	elif na < 0 and nb >= 0:
		return 1
	
	sa = la.find('.sfv')
	sb = lb.find('.sfv')
	
	if sa >= 0 and sb < 0:
		return -1
	elif sa < 0 and sb >= 0:
		return 1
	else:
		return cmp(a, b)

# ---------------------------------------------------------------------------

def NiceSize(bytes):
	bytes = float(bytes)
	
	if bytes < 1024:
		return '<1KB'
	elif bytes < (1024 * 1024):
		return '%dKB' % (bytes / 1024)
	else:
		return '%.1fMB' % (bytes / 1024.0 / 1024.0)

# ---------------------------------------------------------------------------

YSPLIT_RE = re.compile(r'(\S+)=')
def ySplit(line):
	'Split a =y* line into key/value pairs'
	fields = {}
	
	parts = YSPLIT_RE.split(line)[1:]
	if len(parts) % 2:
		return fields
	
	for i in range(0, len(parts), 2):
		key, value = parts[i], parts[i+1]
		fields[key] = value.strip()
	
	return fields

# ---------------------------------------------------------------------------

# Build the yEnc decode table
YDEC_TRANS = ''.join([chr((i + 256 - 42) % 256) for i in range(256)])

def yDecode(data):
	# unescape NUL, TAB, LF, CR, =
	for i in (0, 9, 10, 13, 61):
		j = '=%c' % (i + 64)
		data = data.replace(j, chr(i))
	
	return data.translate(YDEC_TRANS)

# --------------------------------------------------------------------------- 	 