from __future__ import print_function, absolute_import, division
# for localized messages
from . import _, removeBad

# Plugins Config
from xml.etree.cElementTree import parse as cet_parse, fromstring as cet_fromstring
from os import path as os_path, rename as os_rename
from .AutoTimerConfiguration import parseConfig, buildConfig

# Tasks
import Components.Task

# Navigation (RecordTimer)
import NavigationInstance

# Timer
from ServiceReference import ServiceReference
from RecordTimer import RecordTimerEntry

# Notifications
from Tools.Notifications import AddPopup
from Screens import Standby
from Screens.MessageBox import MessageBox

# Timespan
from time import localtime, strftime, time, mktime, sleep, ctime
from datetime import timedelta, date
from Tools.FuzzyDate import FuzzyTime

# EPGCache & Event
from enigma import eEPGCache, eServiceReference, eServiceCenter, iServiceInformation

# AutoTimer Component
from .AutoTimerComponent import preferredAutoTimerComponent

from itertools import chain
from collections import defaultdict
from difflib import SequenceMatcher
from operator import itemgetter

from Plugins.SystemPlugins.Toolkit.SimpleThread import SimpleThread

try:
	from Plugins.Extensions.SeriesPlugin.plugin import renameTimer
except ImportError as ie:
	renameTimer = None

from . import config, xrange, itervalues

from six.moves import range

from six import PY2


XML_CONFIG = "/etc/enigma2/autotimer.xml"

TAG = "AutoTimer"

NOTIFICATIONID = 'AutoTimerNotification'
CONFLICTNOTIFICATIONID = 'AutoTimerConflictEncounteredNotification'
SIMILARNOTIFICATIONID = 'AutoTimerSimilarUsedNotification'


def timeSimilarityPercent(rtimer, evtBegin, evtEnd, timer=None):
	#print("rtimer [",rtimer.begin,",",rtimer.end,"] (",rtimer.end-rtimer.begin," s) - evt [",evtBegin,",",evtEnd,"] (",evtEnd-evtBegin," s)")
	if (timer is not None) and (timer.offset is not None):
		# remove custom offset from rtimer using timer.offset as RecordTimerEntry doesn't store the offset
		# ('evtBegin' and 'evtEnd' are also without offset)
		rtimerBegin = rtimer.begin + timer.offset[0]
		rtimerEnd = rtimer.end - timer.offset[1]
	else:
		# remove E2 offset
		rtimerBegin = rtimer.begin + config.recording.margin_before.value * 60
		rtimerEnd = rtimer.end - config.recording.margin_after.value * 60
	#print("rtimer [",rtimerBegin,",",rtimerEnd,"] (",rtimerEnd-rtimerBegin," s) after removing offsets")
	if (rtimerBegin <= evtBegin) and (evtEnd <= rtimerEnd):
		commonTime = evtEnd - evtBegin
	elif (evtBegin <= rtimerBegin) and (rtimerEnd <= evtEnd):
		commonTime = rtimerEnd - rtimerBegin
	elif evtBegin <= rtimerBegin <= evtEnd:
		commonTime = evtEnd - rtimerBegin
	elif rtimerBegin <= evtBegin <= rtimerEnd:
		commonTime = rtimerEnd - evtBegin
	else:
		commonTime = 0
	if evtBegin != evtEnd:
		commonTime_percent = 100 * commonTime // (evtEnd - evtBegin)
	else:
		return 0
	if rtimerEnd != rtimerBegin:
		durationMatch_percent = 100 * (evtEnd - evtBegin) // (rtimerEnd - rtimerBegin)
	else:
		return 0
	#print("commonTime_percent = ",commonTime_percent,", durationMatch_percent = ",durationMatch_percent)
	if durationMatch_percent < commonTime_percent:
		#avoid false match for a short event completely inside a very long rtimer's time span
		return durationMatch_percent
	else:
		return commonTime_percent


typeMap = {
	"exact": eEPGCache.EXAKT_TITLE_SEARCH,
	"partial": eEPGCache.PARTIAL_TITLE_SEARCH,
	"start": eEPGCache.START_TITLE_SEARCH,
	"description": -99
}

caseMap = {
	"sensitive": eEPGCache.CASE_CHECK,
	"insensitive": eEPGCache.NO_CASE_CHECK
}


class AutoTimer:
	"""Read and save xml configuration, query EPGCache"""

	def __init__(self):
		# Initialize
		self.timers = []
		self.configMtime = -1
		self.nextTimerId = 1
		self.defaultTimer = preferredAutoTimerComponent(
			0,		# Id
			"",		# Name
			"",		# Match
			True 	# Enabled
		)

	# Configuration
	def readXml(self, **kwargs):
		if "xml_string" in kwargs:
			# reset time
			self.configMtime = -1
			# Parse Config
			configuration = cet_fromstring(kwargs["xml_string"])
			# TODO : check config and create backup if wrong
		else:

			# Abort if no config found
			if not os_path.exists(XML_CONFIG):
				print("[AutoTimer] No configuration file present")
				return

			# Parse if mtime differs from whats saved
			mtime = os_path.getmtime(XML_CONFIG)
			if mtime == self.configMtime:
				print("[AutoTimer] No changes in configuration, won't parse")
				return

			# Save current mtime
			self.configMtime = mtime

			# Parse Config
			try:
				configuration = cet_parse(XML_CONFIG).getroot()
			except Exception as error:
				print("An exception occurred:", error)
				try:
					if os_path.exists(XML_CONFIG + "_old"):
						os_rename(XML_CONFIG + "_old", XML_CONFIG + "_old(1)")
					os_rename(XML_CONFIG, XML_CONFIG + "_old")
					print("[AutoTimer] autotimer.xml is corrupt rename file to /etc/enigma2/autotimer.xml_old")
				except:
					pass
				if Standby.inStandby is None:
					AddPopup(_("The autotimer file (/etc/enigma2/autotimer.xml) is corrupt. A new and empty config was created. A backup of the config can be found here (/etc/enigma2/autotimer.xml_old) "), type=MessageBox.TYPE_ERROR, timeout=0, id="AutoTimerLoadFailed")

				self.timers = []
				self.defaultTimer = preferredAutoTimerComponent(
					0,		# Id
					"",		# Name
					"",		# Match
					True  # Enabled
				)

				try:
					self.writeXml()
					configuration = cet_parse(XML_CONFIG).getroot()
				except:
					print("[AutoTimer] fatal error, the autotimer.xml cannot create")
					return

		# Empty out timers and reset Ids
		del self.timers[:]
		self.defaultTimer.clear(-1, True)

		parseConfig(
			configuration,
			self.timers,
			configuration.get("version"),
			0,
			self.defaultTimer
		)
		self.nextTimerId = int(configuration.get("nextTimerId", "0"))
		if not self.nextTimerId:
			self.nextTimerId = len(self.timers) + 1

	def getXml(self, webif=True):
		return buildConfig(self.defaultTimer, self.timers, webif)

	def writeXml(self):
		file = open(XML_CONFIG, 'w')
		file.writelines(buildConfig(self.defaultTimer, self.timers))
		file.close()

	def writeXmlTimer(self, timers):
		return ''.join(buildConfig(self.defaultTimer, timers))

	def readXmlTimer(self, xml_string):
		# Parse xml string
		configuration = cet_fromstring(xml_string)

		parseConfig(
			configuration,
			self.timers,
			configuration.get("version"),
			self.getUniqueId(),
			self.defaultTimer
		)

		# reset time
		self.configMtime = -1

	# Manage List
	def add(self, timer):
		self.timers.append(timer)

	def getEnabledTimerList(self):
		return sorted([x for x in self.timers if x.enabled], key=lambda x: x.name)

	def getTimerList(self):
		return self.timers

	def getTupleTimerList(self):
		lst = self.timers
		return [(x,) for x in lst]

	def getSortedTupleTimerList(self):
		lst = self.timers[:]
		lst.sort()
		return [(x,) for x in lst]

	def getUniqueId(self):
		newId = self.nextTimerId
		self.nextTimerId += 1
		return newId

	def remove(self, uniqueId):
		idx = 0
		for timer in self.timers:
			if timer.id == uniqueId:
				self.timers.pop(idx)
				return
			idx += 1

	def set(self, timer):
		idx = 0
		for stimer in self.timers:
			if stimer == timer:
				self.timers[idx] = timer
				return
			idx += 1
		self.timers.append(timer)

	#call from epgrefresh
	def parseEPGAsync(self, simulateOnly=False):
		t = SimpleThread(lambda: self.parseEPG(simulateOnly=simulateOnly))
		t.start()
		return t.deferred

	# Main function
	def parseEPG(self, autoPoll=False, simulateOnly=False, uniqueId=None, callback=None):
		self.autoPoll = autoPoll
		self.simulateOnly = simulateOnly
		self.testid = uniqueId

		self.new = 0
		self.modified = 0
		self.skipped = []
		self.existing = []
		self.total = 0
		self.autotimers = []
		self.conflicting = []
		self.similars = []
		self.callback = callback

		# NOTE: the config option specifies "the next X days" which means today (== 1) + X
		delta = timedelta(days=config.plugins.autotimer.maxdaysinfuture.getValue() + 1)
		self.evtLimit = mktime((date.today() + delta).timetuple())
		self.checkEvtLimit = delta.days > 1
		del delta

		# Read AutoTimer configuration
		self.readXml()

		# Get E2 instances
		self.epgcache = eEPGCache.getInstance()
		self.serviceHandler = eServiceCenter.getInstance()
		self.recordHandler = NavigationInstance.instance.RecordTimer

		# Save Timer in a dict to speed things up a little
		# We include processed timers as we might search for duplicate descriptions
		# NOTE: It is also possible to use RecordTimer isInTimer(), but we won't get the timer itself on a match
		self.timerdict = defaultdict(list)
		self.populateTimerdict(self.epgcache, self.recordHandler, self.timerdict)

		# Create dict of all movies in all folders used by an autotimer to compare with recordings
		# The moviedict will be filled only if one AutoTimer is configured to avoid duplicate description for any recordings
		self.moviedict = defaultdict(list)

		# Iterate Timer
		Components.Task.job_manager.AddJob(self.createTask())

	def createTask(self):
		self.timer_count = 0
		self.completed = []
		self.searchtimer = []
		job = Components.Task.Job(_("AutoTimer"))
		timer = None

		# Iterate Timer
		for timer in self.getEnabledTimerList():
			# test only timer with specific id
			if self.testid:
				if self.testid != timer.id:
					continue
			taskname = timer.name + '_%d' % self.timer_count
			task = Components.Task.PythonTask(job, taskname)
			self.searchtimer.append((timer, taskname))
			task.work = self.JobStart
			task.weighting = 1
			self.timer_count += 1

		if timer:
			task = Components.Task.PythonTask(job, 'Show results')
			task.work = self.JobMessage
			task.weighting = 1

		return job

	def JobStart(self):
		for timer, taskname in self.searchtimer:
			if taskname not in self.completed:
				self.parseTimer(timer, self.epgcache, self.serviceHandler, self.recordHandler, self.checkEvtLimit, self.evtLimit, self.autotimers, self.conflicting, self.similars, self.skipped, self.existing, self.timerdict, self.moviedict, taskname, self.simulateOnly)
				self.new += self.result[0]
				self.modified += self.result[1]
				break

	def parseTimer(self, timer, epgcache, serviceHandler, recordHandler, checkEvtLimit, evtLimit, timers, conflicting, similars, skipped, existing, timerdict, moviedict, taskname, simulateOnly=False):
		new = 0
		modified = 0

		# enable multiple timer if services or bouquets specified (eg. recording the same event on sd service and hd service)
		enable_multiple_timer = ((timer.services and 's' in config.plugins.autotimer.enable_multiple_timer.value or False) or (timer.bouquets and 'b' in config.plugins.autotimer.enable_multiple_timer.value or False))

		# Precompute timer destination dir
		dest = timer.destination or config.usage.default_path.value

		# Workaround to allow search for umlauts if we know the encoding
		match = removeBad(timer.match)
		if timer.encoding != 'UTF-8':
			try:
				if PY2:
					match = match.decode('UTF-8').encode(timer.encoding)  # FIXME PY3
			except UnicodeDecodeError:
				pass

		self.isIPTV = bool([service for service in timer.services if "%3a//" in service])

		# As well as description, also allow timers on individual IPTV streams
		if timer.searchType == "description" or self.isIPTV:
			epgmatches = []

			casesensitive = timer.searchCase == "sensitive"
			if not casesensitive:
				match = match.lower()

			test = []
			if timer.services:
				test = [(service, 0, -1, -1) for service in timer.services]
			elif timer.bouquets:
				for bouquet in timer.bouquets:
					services = serviceHandler.list(eServiceReference(bouquet))
					if services:
						while True:
							service = services.getNext()
							if not service.valid():
								break
							playable = not (service.flags & (eServiceReference.isMarker | eServiceReference.isDirectory))
							if playable:
								test.append((service.toString(), 0, -1, -1))
			else:  # Get all bouquets
				bouquetlist = []
				refstr = '1:134:1:0:0:0:0:0:0:0:FROM BOUQUET \"bouquets.tv\" ORDER BY bouquet'
				bouquetroot = eServiceReference(refstr)
				mask = eServiceReference.isDirectory
				if config.usage.multibouquet.value:
					bouquets = serviceHandler.list(bouquetroot)
					if bouquets:
						while True:
							s = bouquets.getNext()
							if not s.valid():
								break
							if s.flags & mask:
								info = serviceHandler.info(s)
								if info:
									bouquetlist.append(s)
				else:
					info = serviceHandler.info(bouquetroot)
					if info:
						bouquetlist.append(bouquetroot)
				if bouquetlist:
					for bouquet in bouquetlist:
						if not bouquet.valid():
							continue
						if bouquet.flags & eServiceReference.isDirectory:
							services = serviceHandler.list(bouquet)
							if services:
								while True:
									service = services.getNext()
									if not service.valid():
										break
									playable = not (service.flags & (eServiceReference.isMarker | eServiceReference.isDirectory))
									if playable:
										test.append((service.toString(), 0, -1, -1))

			if test:
				# Get all events
				#  eEPGCache.lookupEvent( [ format of the returned tuples, ( service, 0 = event intersects given start_time, start_time -1 for now_time), ] )
				test.insert(0, 'RITBDSE')
				allevents = epgcache.lookupEvent(test) or []

				# Filter events
				for serviceref, eit, name, begin, duration, shortdesc, extdesc in allevents:
					if timer.searchType == "description":
						if match in (shortdesc if casesensitive else shortdesc.lower()) or match in (extdesc if casesensitive else extdesc.lower()):
							epgmatches.append((serviceref, eit, name, begin, duration, shortdesc, extdesc))
					else:  # IPTV streams (if not "description" search)
						if timer.searchType == 'exact' and match == (name if casesensitive else name.lower()) or \
							timer.searchType == 'partial' and match in (name if casesensitive else name.lower()) or \
							timer.searchType == 'start' and (name if casesensitive else name.lower()).startswith(match):
							epgmatches.append((serviceref, eit, name, begin, duration, shortdesc, extdesc))

		else:
			# Search EPG, default to empty list
			if timer.searchType in typeMap:
				EPG_searchType = typeMap[timer.searchType]
			else:
				EPG_searchType = typeMap["partial"]
			epgmatches = epgcache.search(('RITBDSE', 3000, EPG_searchType, match, caseMap[timer.searchCase])) or []

		# Sort list of tuples by begin time 'B'
		epgmatches.sort(key=itemgetter(3))

		# Contains the the marked similar eits and the conflicting strings
		similardict = defaultdict(list)

		# Loop over all EPG matches
		preveit = False
		for idx, (serviceref, eit, name, begin, duration, shortdesc, extdesc) in enumerate(epgmatches):

			eserviceref = eServiceReference(serviceref)
			evt = epgcache.lookupEventId(eserviceref, eit)
			evtBegin = begin
			evtEnd = end = begin + duration

			if not evt:
				msg = "[AutoTimer] Could not create Event!"
				print(msg)
				skipped.append((name, begin, end, str(serviceref), timer.name, msg))
				continue
			# Try to determine real service (we always choose the last one)
			n = evt.getNumOfLinkageServices()
			if n > 0:
				i = evt.getLinkageService(eserviceref, n - 1)
				serviceref = i.toString()

			# If event is expired skip it
			if end < time():
			#	print("[AutoTimer] Skipping expired timer")
				continue

			# If event starts in less than 60 seconds skip it
			# if begin < time() + 60:
			# 	print ("[AutoTimer] Skipping " + name + " because it starts in less than 60 seconds")
			# 	skipped += 1
			# 	continue

			# Set short description to equal extended description if it is empty.
			if not shortdesc:
				shortdesc = extdesc

			# Convert begin time
			timestamp = localtime(begin)
			# Update timer
			timer.update(begin, timestamp)

			# Check if eit is in similar matches list
			# NOTE: ignore evtLimit for similar timers as I feel this makes the feature unintuitive
			similarTimer = False
			if eit in similardict:
				similarTimer = True
				dayofweek = None  # NOTE: ignore day on similar timer
			else:
				# If maximum days in future is set then check time
				if checkEvtLimit:
					if begin > evtLimit:
						msg = "[AutoTimer] Skipping an event because of maximum days in future is reached"
#						print(msg)
						skipped.append((name, begin, end, serviceref, timer.name, msg))
						continue

# If the timer actually has a timespan set it will be:
#   start[[hr], [min]], end[[hr], [min]], daySpan
# (if it has none the timespan will be (None,))
# (see calculateDayspan() in AutoTimerComponent.py) where daySpan is
# True if the timespan "ends before it starts" (so passes over
# midnight).
# If we have a timer for which daySpan is true and the day-offset of the
# begin time for the programme/broadcast we are checking is before that
# of the autotimer timespan start then we need to bring the dayofweek
# check forward by 1 day when checking it. (i.e. we pretend the
# recording starts a day before it does, but *just* for the dayofweek
# filter check).
#   e.g.
#       Monday AT for 23:00 to 02:00 should match a programme on
#       Tuesday at 01:00.
# The rest of the checks stay the same (which is why we don't need to
# check for the autotimer timespan end being after the begin time for
# the programme).
#
				tdow = timestamp.tm_wday
				if (timer.timespan[0] is not None) and timer.timespan[2]:
					begin_offset = 60 * timestamp.tm_hour + timestamp.tm_min
					timer_offset = 60 * timer.timespan[0][0] + timer.timespan[0][1]
					if begin_offset < timer_offset:
						tdow = (tdow - 1) % 7
				dayofweek = str(tdow)

			# Check timer conditions
			# NOTE: similar matches do not care about the day/time they are on, so ignore them
			if timer.checkServices(serviceref) \
				or timer.checkDuration(duration) \
				or (not similarTimer and (
					timer.checkTimespan(timestamp)
					or timer.checkTimeframe(begin)
				)) or timer.checkFilter(name, shortdesc, extdesc, dayofweek):
				msg = "[AutoTimer] Skipping an event because of filter check"
#				print(msg)
				skipped.append((name, begin, end, serviceref, timer.name, msg))
				continue

			if timer.hasOffset():
				# Apply custom Offset
				begin, end = timer.applyOffset(begin, end)
				offsetBegin = timer.offset[0]
				offsetEnd = timer.offset[1]
			else:
				# Apply E2 Offset
				marginBefore = config.recording.margin_before.value * 60
				marginAfter = config.recording.margin_after.value * 60
				if timer.justplay and hasattr(config.recording, "zap_margin_before"):
					marginBefore = config.recording.zap_margin_before.value * 60
					marginAfter = config.recording.zap_margin_after.value * 60
				begin -= marginBefore
				end += marginAfter
				offsetBegin = marginBefore
				offsetEnd = marginAfter

			# Overwrite endtime if requested
			if timer.justplay and not timer.setEndtime:
				end = begin
				offsetEnd = 0

			# Eventually change service to alternative
			if timer.overrideAlternatives:
				serviceref = timer.getAlternative(serviceref)

			# Append to timerlist and abort if simulating
			timers.append((name, begin, end, serviceref, timer.name))
			if simulateOnly:
				continue

			# Check for existing recordings in directory
			if timer.avoidDuplicateDescription == 3:
				# Reset movie Exists
				movieExists = False

				if dest and dest not in moviedict:
					self.addDirectoryToMovieDict(moviedict, dest, serviceHandler)
				for movieinfo in moviedict.get(dest, ()):
					if self.checkSimilarity(timer, name, movieinfo.get("name"), shortdesc, movieinfo.get("shortdesc"), extdesc, movieinfo.get("extdesc")):
						print("[AutoTimer] We found a matching recorded movie, skipping event:", name)
						movieExists = True
						break
				if movieExists:
					msg = "[AutoTimer] Skipping an event because movie already exists"
#					print(msg)
					skipped.append((name, begin, end, serviceref, timer.name, msg))
					continue

			# Initialize
			newEntry = None
			oldExists = False

			# Check for double Timers
			# We first check eit and if user wants us to guess event based on time
			# we try this as backup. The allowed diff should be configurable though.
			for rtimer in timerdict.get(serviceref, ()):
				if rtimer.eit == eit or (config.plugins.autotimer.try_guessing.getValue() and timeSimilarityPercent(rtimer, evtBegin, evtEnd, timer) > 80):
					oldExists = True

					# Abort if we don't want to modify timers or timer is repeated
					if config.plugins.autotimer.refresh.value == "none" or rtimer.repeated:
#						print("[AutoTimer] Won't modify existing timer because either no modification allowed or repeated timer")
						break

					if eit == preveit:
						break
					try:  # protect against vps plugin not being present
						vps_changed = rtimer.vpsplugin_enabled != timer.vps_enabled or rtimer.vpsplugin_overwrite != timer.vps_overwrite
					except AttributeError:
						vps_changed = False
					if (evtBegin - offsetBegin != rtimer.begin) or (evtEnd + offsetEnd != rtimer.end) or (shortdesc != rtimer.description) or vps_changed:
						if rtimer.isAutoTimer and eit == rtimer.eit:
							print("[AutoTimer] AutoTimer %s modified this automatically generated timer." % (timer.name))
							# rtimer.log(501, "[AutoTimer] AutoTimer %s modified this automatically generated timer." % (timer.name))
							preveit = eit
						else:
							if config.plugins.autotimer.refresh.getValue() != "all":
								print("[AutoTimer] Won't modify existing timer because it's no timer set by us")
								break
							rtimer.log(501, "[AutoTimer] Warning, AutoTimer %s messed with a timer which might not belong to it: %s ." % (timer.name, rtimer.name))
						newEntry = rtimer
						modified += 1
						self.modifyTimer(rtimer, name, shortdesc, begin, end, serviceref, eit, offsetBegin, offsetEnd)
						# rtimer.log(501, "[AutoTimer] AutoTimer modified timer: %s ." % (rtimer.name))
						break
					else:
#						print("[AutoTimer] Skipping timer because it has not changed.")
						existing.append((name, begin, end, serviceref, timer.name))
						break
				elif timer.avoidDuplicateDescription >= 1 and not rtimer.disabled:
					if self.checkSimilarity(timer, name, rtimer.name, shortdesc, rtimer.description, extdesc, rtimer.extdesc):
						print("[AutoTimer] We found a timer with similar description, skipping event")
						oldExists = True
						break

			# We found no timer we want to edit
			if newEntry is None:
				# But there is a match
				if oldExists:
					continue

				# We want to search for possible doubles
				for rtimer in chain.from_iterable(itervalues(timerdict)):
					if not rtimer.disabled:
						if self.checkDoubleTimers(timer, name, rtimer.name, begin, rtimer.begin, end, rtimer.end, serviceref, str(rtimer.service_ref), enable_multiple_timer):
							oldExists = True
							print("[AutoTimer] We found a timer with same StartTime, skipping event")
							break
						if timer.avoidDuplicateDescription >= 2:
							if self.checkSimilarity(timer, name, rtimer.name, shortdesc, rtimer.description, extdesc, rtimer.extdesc):
								oldExists = True
								# print("[AutoTimer] We found a timer (any service) with same description, skipping event")
								break
				if oldExists:
					continue

				if timer.checkCounter(timestamp):
					print("[AutoTimer] Not adding new timer because counter is depleted.")
					continue

				newEntry = RecordTimerEntry(ServiceReference(serviceref), begin, end, name, shortdesc, eit)
				newEntry.log(500, "[AutoTimer] Try to add new timer based on AutoTimer %s." % (timer.name))
				newEntry.log(509, "[AutoTimer] Timer start on: %s" % ctime(begin))

				# Mark this entry as AutoTimer (only AutoTimers will have this Attribute set)
				newEntry.isAutoTimer = True
				newEntry.autoTimerId = timer.id

				# set the correct margins
				if hasattr(newEntry, "marginBefore"):
					newEntry.marginBefore = offsetBegin
					newEntry.marginAfter = offsetEnd
					newEntry.eventBegin = newEntry.begin + offsetBegin
					newEntry.eventEnd = newEntry.end - offsetEnd

			# Apply afterEvent
			if timer.hasAfterEvent():
				afterEvent = timer.getAfterEventTimespan(localtime(end))
				if afterEvent is None:
					afterEvent = timer.getAfterEvent()
				if afterEvent is not None:
					newEntry.afterEvent = afterEvent

			newEntry.dirname = timer.destination
			newEntry.justplay = timer.justplay
			newEntry.hasEndTime = timer.setEndtime
			newEntry.vpsplugin_enabled = timer.vps_enabled
			newEntry.vpsplugin_overwrite = timer.vps_overwrite

			if hasattr(timer, 'always_zap') and hasattr(newEntry, 'always_zap'):
				newEntry.always_zap = timer.always_zap
			tags = timer.tags[:]
			if config.plugins.autotimer.add_autotimer_to_tags.value:
				if TAG not in tags:
					tags.append(TAG)
			if config.plugins.autotimer.add_name_to_tags.value:
				tagname = timer.name.strip()
				if tagname:
					tagname = tagname[0].upper() + tagname[1:].replace(" ", "_")
					if tagname not in tags:
						tags.append(tagname)
			newEntry.tags = tags

			if oldExists:
				# XXX: this won't perform a sanity check, but do we actually want to do so?
				recordHandler.timeChanged(newEntry)

				if renameTimer is not None and timer.series_labeling:
					renameTimer(newEntry, name, evtBegin, evtEnd)

			else:
				conflictString = ""
				if similarTimer:
					conflictString = similardict[eit].conflictString
					msg = "[AutoTimer] Try to add similar Timer because of conflicts with %s." % (conflictString)
					print(msg)
					newEntry.log(504, msg)

				# Try to add timer
				conflicts = recordHandler.record(newEntry)

				if conflicts and not timer.hasOffset() and not config.recording.margin_before.value and not config.recording.margin_after.value and len(conflicts) > 1:
					change_end = change_begin = False
					conflict_begin = conflicts[1].begin
					conflict_end = conflicts[1].end
					if conflict_begin == newEntry.end:
						newEntry.end -= 30
						change_end = True
					elif newEntry.begin == conflict_end:
						newEntry.begin += 30
						change_begin = True
					if change_end or change_begin:
						conflicts = recordHandler.record(newEntry)
						if conflicts:
							if change_end:
								newEntry.end += 30
							elif change_begin:
								newEntry.begin -= 30
						else:
							print("[AutoTimer] The conflict is resolved by offset time begin/end (30 sec) for %s." % newEntry.name)

				if conflicts:
					# Maybe use newEntry.log
					conflictString += ' / '.join(["%s (%s)" % (x.name, strftime("%Y%m%d %H%M", localtime(x.begin))) for x in conflicts])
					print("[AutoTimer] conflict with %s detected" % (conflictString))

					if config.plugins.autotimer.addsimilar_on_conflict.value:
						# We start our search right after our actual index
						# Attention we have to use a copy of the list, because we have to append the previous older matches
						lepgm = len(epgmatches)
						for i in range(lepgm):
							servicerefS, eitS, nameS, beginS, durationS, shortdescS, extdescS = epgmatches[(i + idx + 1) % lepgm]
							if self.checkSimilarity(timer, name, nameS, shortdesc, shortdescS, extdesc, extdescS, force=True):
								# Check if the similar is already known
								if eitS not in similardict:
									print("[AutoTimer] Found similar Timer: " + name)

									# Store the actual and similar eit and conflictString, so it can be handled later
									newEntry.conflictString = conflictString
									similardict[eit] = newEntry
									similardict[eitS] = newEntry
									similarTimer = True
									if beginS <= evtBegin:
										# Event is before our actual epgmatch so we have to append it to the epgmatches list
										epgmatches.append((servicerefS, eitS, nameS, beginS, durationS, shortdescS, extdescS))
									# If we need a second similar it will be found the next time
								else:
									similarTimer = False
									newEntry = similardict[eitS]
								break

				if conflicts is None:
					timer.decrementCounter()
					new += 1
					newEntry.extdesc = extdesc
					timerdict[serviceref].append(newEntry)

					if renameTimer is not None and timer.series_labeling:
						renameTimer(newEntry, name, evtBegin, evtEnd)

					# Similar timers are in new timers list and additionally in similar timers list
					if similarTimer:
						similars.append((name, begin, end, serviceref, timer.name))
						similardict.clear()

				# Don't care about similar timers
				elif not similarTimer:
					conflicting.append((name, begin, end, serviceref, timer.name))

					if config.plugins.autotimer.disabled_on_conflict.value:
						msg = "[AutoTimer] Timer disabled because of conflicts with %s." % (conflictString)
						print(msg)
						newEntry.log(503, msg)
						newEntry.disabled = True
						# We might want to do the sanity check locally so we don't run it twice - but I consider this workaround a hack anyway
						conflicts = recordHandler.record(newEntry)
		self.result = (new, modified)
		self.completed.append(taskname)
		sleep(0.5)

	def JobMessage(self):
		if self.callback is not None:
			if self.simulateOnly is True:
				self.callback(self.autotimers, self.skipped)
			else:
				total = (self.new + self.modified + len(self.conflicting) + len(self.existing) + len(self.similars))
				_result = (total, self.new, self.modified, self.autotimers, self.conflicting, self.similars, self.existing, self.skipped)
				self.callback(_result)
		elif self.autoPoll:
			if self.conflicting and config.plugins.autotimer.notifconflict.value:
				AddPopup(
					_("%d conflict(s) encountered when trying to add new timers:\n%s") % (len(self.conflicting), '\n'.join([_("%s: %s at %s") % (x[4], x[0], FuzzyTime(x[2])) for x in self.conflicting])),
					MessageBox.TYPE_INFO,
					config.plugins.autotimer.popup_timeout.value,
					CONFLICTNOTIFICATIONID
				)
			elif self.similars and config.plugins.autotimer.notifsimilar.value:
				AddPopup(
					_("%d conflict(s) solved with similar timer(s):\n%s") % (len(self.similars), '\n'.join([_("%s: %s at %s") % (x[4], x[0], FuzzyTime(x[2])) for x in self.similars])),
					MessageBox.TYPE_INFO,
					config.plugins.autotimer.popup_timeout.value,
					SIMILARNOTIFICATIONID
				)
		else:
			AddPopup(
				_("Found a total of %d matching Events.\n%d Timer were added and\n%d modified,\n%d conflicts encountered,\n%d unchanged,\n%d similars added.") % ((self.new + self.modified + len(self.conflicting) + len(self.existing) + len(self.similars)), self.new, self.modified, len(self.conflicting), len(self.existing), len(self.similars)),
				MessageBox.TYPE_INFO,
				config.plugins.autotimer.popup_timeout.value,
				NOTIFICATIONID
			)

# Supporting functions

	def populateTimerdict(self, epgcache, recordHandler, timerdict):
#		remove = []
		for timer in chain(recordHandler.timer_list, recordHandler.processed_timers):
			if timer and timer.service_ref:
				if timer.eit is not None:
					event = epgcache.lookupEventId(timer.service_ref.ref, timer.eit)
					if event:
						timer.extdesc = event.getExtendedDescription() or ''
					else:
						timer.extdesc = ''
#						remove.append(timer)
				elif not hasattr(timer, 'extdesc'):
					timer.extdesc = ''
#				else:
#					remove.append(timer)
#					continue
				timerdict[str(timer.service_ref)].append(timer)

#		if config.plugins.autotimer.check_eit_and_remove.value:
#			for timer in remove:
#				if "autotimer" in timer.flags:
#					try:
#						# Because of the duplicate check, we only want to remove future timer
#						if timer in recordHandler.timer_list:
#							if not timer.isRunning():
#								recordHandler.removeEntry(timer)
#								print("[AutoTimer] Remove timer because of eit check %s." % (timer.name))
#					except:
#						pass
#		del remove

	def modifyTimer(self, timer, name, shortdesc, begin, end, serviceref, eit, offsetBegin, offsetEnd):
		# Don't update the name, it will overwrite the name of the SeriesPlugin
		#timer.name = name

		timer.description = shortdesc
		timer.begin = int(begin)
		timer.end = int(end)
		timer.service_ref = ServiceReference(serviceref)
		timer.eit = eit

		if hasattr(timer, "marginBefore"):
			timer.marginBefore = offsetBegin
			timer.marginAfter = offsetEnd
			timer.eventBegin = timer.begin + offsetBegin
			timer.eventEnd = timer.end - offsetEnd

	def addDirectoryToMovieDict(self, moviedict, dest, serviceHandler):
		movielist = serviceHandler.list(eServiceReference("2:0:1:0:0:0:0:0:0:0:" + dest))
		if movielist is None:
			print("[AutoTimer] listing of movies in " + dest + " failed")
		else:
			append = moviedict[dest].append
			while True:
				movieref = movielist.getNext()
				if not movieref.valid():
					break
				if movieref.flags & eServiceReference.mustDescent:
					continue
				info = serviceHandler.info(movieref)
				if info is None:
					continue
				event = info.getEvent(movieref)
				if event is None:
					continue
				append({
					"name": info.getName(movieref),
					"shortdesc": info.getInfoString(movieref, iServiceInformation.sDescription),
					"extdesc": event.getExtendedDescription() or ''  # XXX: does event.getExtendedDescription() actually return None on no description or an empty string?
				})

	def checkSimilarity(self, timer, name1, name2, shortdesc1, shortdesc2, extdesc1, extdesc2, force=False):
		foundTitle = False
		foundShort = False
		retValue = False
		if name1 and name2:
			foundTitle = (0.8 < SequenceMatcher(lambda x: x == " ", name1, name2).ratio())
		# NOTE: only check extended & short if tile is a partial match
		if foundTitle:
			if timer.searchForDuplicateDescription > 0 or force:
				if shortdesc1 and shortdesc2:
					# If the similarity percent is higher then 0.7 it is a very close match
					foundShort = (0.7 < SequenceMatcher(lambda x: x == " ", shortdesc1, shortdesc2).ratio())
					if foundShort:
						# At this point we assume the similarity match to be True
						# unless we have been asked to check Extended Descriptions
						# and we have *both* Extended Descriptions in place;
						# in which case we test them.
						retValue = True
						if timer.searchForDuplicateDescription == 2:
							if extdesc1 and extdesc2:
								# Some channels indicate replays in the extended descriptions
								# If the similarity percent is higher then 0.7 it is a very close match
								retValue = (0.7 < SequenceMatcher(lambda x: x == " ", extdesc1, extdesc2).ratio())
			else:
				retValue = True
		return retValue

	def checkDoubleTimers(self, timer, name1, name2, starttime1, starttime2, endtime1, endtime2, serviceref1, serviceref2, multiple):
		foundTitle = name1 == name2
		foundstart = starttime1 == starttime2
		foundend = endtime1 == endtime2
		foundref = serviceref1 == serviceref2
		return foundTitle and foundstart and foundend and (foundref or not multiple)
