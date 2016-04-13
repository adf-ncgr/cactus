#!/usr/bin/env python

#Copyright (C) 2009-2011 by Benedict Paten (benedictpaten@gmail.com)
#
#Released under the MIT license, see LICENSE.txt
#!/usr/bin/env python

"""Script strings together all the components to make the basic pipeline for reconstruction.

The script uses the the jobTree.scriptTree target framework to structure all the related wrappers.
"""

import os
import sys
import xml.etree.ElementTree as ET
import math
import time
import bz2
import random
import copy
import shutil
from argparse import ArgumentParser

from sonLib.bioio import getTempFile
from sonLib.bioio import newickTreeParser

from sonLib.bioio import logger
from toil.lib.bioio import setLoggingFromOptions
from sonLib.bioio import system
from sonLib.bioio import makeSubDir

from cactus.shared.common import cactusRootPath
  
from toil.job import Job

from jobTree.src.bioio import getLogLevelString

from cactus.shared.common import getOptionalAttrib
from cactus.shared.common import runCactusSetup
from cactus.shared.common import runCactusCaf
from cactus.shared.common import runCactusGetFlowers
from cactus.shared.common import runCactusExtendFlowers
from cactus.shared.common import runCactusSplitFlowersBySecondaryGrouping
from cactus.shared.common import encodeFlowerNames
from cactus.shared.common import decodeFirstFlowerName
from cactus.shared.common import runCactusConvertAlignmentToCactus
from cactus.shared.common import runCactusPhylogeny
from cactus.shared.common import runCactusAdjacencies
from cactus.shared.common import runCactusBar
from cactus.shared.common import runCactusMakeNormal 
from cactus.shared.common import runCactusReference
from cactus.shared.common import runCactusAddReferenceCoordinates
from cactus.shared.common import runCactusCheck
from cactus.shared.common import runCactusHalGenerator
from cactus.shared.common import runCactusFlowerStats
from cactus.shared.common import runCactusSecondaryDatabase
from cactus.shared.common import runCactusFastaGenerator
from cactus.shared.common import findRequiredNode
from cactus.shared.common import runConvertAlignmentsToInternalNames
from cactus.shared.common import runStripUniqueIDs

from cactus.shared.experimentWrapper import ExperimentWrapper
from cactus.blast.cactus_blast import BlastIngroupsAndOutgroups
from cactus.blast.cactus_blast import BlastFlower
from cactus.blast.cactus_blast import BlastOptions

from cactus.preprocessor.cactus_preprocessor import CactusPreprocessor

from cactus.shared.experimentWrapper import ExperimentWrapper
from cactus.shared.experimentWrapper import DbElemWrapper
from cactus.shared.configWrapper import ConfigWrapper
from cactus.pipeline.ktserverToil import KtServerService

############################################################
############################################################
############################################################
##Shared functions
############################################################
############################################################
############################################################


#
#
#
#   cactusTrimmingBlastPhase --> cactusTrimmingBlastPhase2 --> cactusSetupPhase --------> cactusCafPhase ------|
#                                                                    |                          |              |
#                                                                    |                          |              |
#                                                              ktServerJob                 extendFlowers       |
#                                                                   /\                          |              |
#                                                                  /  \                    cactusCafRecursion  |
#                                                                 /    \                        |              |
#                                                           ktServer  cactusSetupPhase2    cactusCafWrapper    |
#                                                                                                              |
#                                                                                                              |
# -------------------------------------------------------------------------------------------------------------|
# |---> ...(more caf phases) -------> cactusBarPhase -------> cactusNormalPhase
#                                           |
#                                      extendFlowers
#                                           |
#                                     cactusBarRecursion
#                                           |
#                                     cactusBarWrapper
#

def extractNode(node):
    """Make an XML node free of its parent subtree
    """
    return ET.fromstring(ET.tostring(node))

def getJobNode(phaseNode, jobClass):
    """Gets a job node for a given job.
    """
    className = jobClass.__name__
    assert className != ''
    assert className.isalnum()
    return phaseNode.find(className)

class CactusJob(Job):
    """Base job for all cactus workflow jobs.
    """
    def __init__(self, phaseNode, constantsNode, overlarge=False):
        self.phaseNode = phaseNode
        self.constantsNode = constantsNode
        self.overlarge = overlarge
        self.jobNode = getJobNode(self.phaseNode, self.__class__)
        if overlarge:
            Job.__init__(self, memory=self.getOptionalJobAttrib("overlargeMemory", typeFn=int, 
                                                                      default=getOptionalAttrib(self.constantsNode, "defaultOverlargeMemory", int, default=sys.maxint)),
                                  cores=self.getOptionalJobAttrib("overlargeCpu", typeFn=int, 
                                                                      default=getOptionalAttrib(self.constantsNode, "defaultOverlargeCpu", int, default=None)))
        else:
            Job.__init__(self, memory=self.getOptionalJobAttrib("memory", typeFn=int, 
                                                                      default=getOptionalAttrib(self.constantsNode, "defaultMemory", int, default=sys.maxint)),
                                  cores=self.getOptionalJobAttrib("cpu", typeFn=int, 
                                                                      default=getOptionalAttrib(self.constantsNode, "defaultCpu", int, default=None)))
    
    def getOptionalPhaseAttrib(self, attribName, typeFn=None, default=None):
        """Gets an optional attribute of the phase node.
        """
        return getOptionalAttrib(node=self.phaseNode, attribName=attribName, typeFn=typeFn, default=default)
    
    def getOptionalJobAttrib(self, attribName, typeFn=None, default=None):
        """Gets an optional attribute of the job node.
        """
        return getOptionalAttrib(node=self.jobNode, attribName=attribName, typeFn=typeFn, default=default)

class CactusPhasesJob(CactusJob):
    """Base job for each workflow phase job.
    """
    def __init__(self, cactusWorkflowArguments, phaseName, topFlowerName=0, index=0):
        phaseNode = findRequiredNode(cactusWorkflowArguments.configNode, phaseName, index)
        constantsNode = findRequiredNode(cactusWorkflowArguments.configNode, "constants")
        CactusJob.__init__(self, phaseNode=phaseNode, constantsNode=constantsNode, overlarge=False)
        self.index = index
        self.cactusWorkflowArguments = cactusWorkflowArguments
        self.topFlowerName = topFlowerName
    
    def makeRecursiveChildJob(self, job, launchSecondaryKtForRecursiveJob=False):
        newChild = job(phaseNode=extractNode(self.phaseNode), 
                          constantsNode=extractNode(self.constantsNode),
                          cactusDiskDatabaseString=self.cactusWorkflowArguments.cactusDiskDatabaseString, 
                          flowerNames=encodeFlowerNames((self.topFlowerName,)), overlarge=True)
        
        if launchSecondaryKtForRecursiveJob and ExperimentWrapper(self.cactusWorkflowArguments.experimentNode).getDbType() == "kyoto_tycoon":
            cw = ConfigWrapper(self.cactusWorkflowArguments.configNode)
            memory = cw.getKtserverMemory(default=getOptionalAttrib(
                    self.constantsNode, "defaultMemory", int, default=sys.maxint))
            cpu = cw.getKtserverCpu(default=getOptionalAttrib(
                    self.constantsNode, "defaultCpu", int, default=sys.maxint))
            dbElem = ExperimentWrapper(self.cactusWorkflowArguments.experimentNode)
            self.addService(KtServerService(dbElem = dbElem, isSecondary = True))
            return self.addChild(newChild).rv()
        else:
            return self.addChild(newChild).rv()
    
    def makeFollowOnPhaseJob(self, job, phaseName, index=0):
        return self.addFollowOn(job(cactusWorkflowArguments=self.cactusWorkflowArguments, phaseName=phaseName, 
                                      topFlowerName=self.topFlowerName, index=index)).rv()
        
    def runPhase(self, recursiveJob, nextPhaseJob, nextPhaseName, doRecursion=True, index=0, launchSecondaryKtForRecursiveJob=False):
        """
        Adds a recursive child job and then a follow-on phase job. Returns the result of the follow-on
        phase job.
        """
        logger.info("Starting %s phase job with index %i at %s seconds (recursing = %i)" % (self.phaseNode.tag, self.getPhaseIndex(), time.time(), doRecursion))
        if doRecursion:
            self.makeRecursiveChildJob(recursiveJob, launchSecondaryKtForRecursiveJob)
        return self.makeFollowOnPhaseJob(job=nextPhaseJob, phaseName=nextPhaseName, index=index)
        
    def getPhaseIndex(self):
        return self.index
    
    def getPhaseNumber(self):
        return len(self.cactusWorkflowArguments.configNode.findall(self.phaseNode.tag))
    
    def setupSecondaryDatabase(self):
        """Setup the secondary database
        """
        confXML = ET.fromstring(self.cactusWorkflowArguments.secondaryDatabaseString)
        dbElem = DbElemWrapper(confXML)
        if dbElem.getDbType() != "kyoto_tycoon":
            runCactusSecondaryDatabase(self.cactusWorkflowArguments.secondaryDatabaseString, create=True)
    
    def cleanupSecondaryDatabase(self):
        """Cleanup the secondary database
        """
        confXML = ET.fromstring(self.cactusWorkflowArguments.secondaryDatabaseString)
        dbElem = DbElemWrapper(confXML)
        if dbElem.getDbType() != "kyoto_tycoon":
            runCactusSecondaryDatabase(self.cactusWorkflowArguments.secondaryDatabaseString, create=False)

class CactusRecursionJob(CactusJob):
    """Base recursive job for traversals up and down the cactus tree.
    """
    maxSequenceSizeOfFlowerGroupingDefault = 1000000
    def __init__(self, phaseNode, constantsNode, cactusDiskDatabaseString, flowerNames, overlarge=False, precomputedAlignmentIDs=None):
        CactusJob.__init__(self, phaseNode=phaseNode, constantsNode=constantsNode, overlarge=overlarge)
        self.cactusDiskDatabaseString = cactusDiskDatabaseString
        self.flowerNames = flowerNames  
        
        #need to do this because the alignment IDs are jobstore promises, and can't 
        #be stored in the config XML until they are respolved into actual IDs, which doesn't
        #happen until the follow-on job after CactusBarWrapperLarge
        self.precomputedAlignmentIDs = precomputedAlignmentIDs
        
    def makeFollowOnRecursiveJob(self, job, phaseNode=None):
        """Sets the followon to the given recursive job
        """
        if phaseNode == None:
            phaseNode = self.phaseNode
        self.addFollowOn(job(phaseNode=phaseNode, constantsNode=self.constantsNode,
                                   cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                                   flowerNames=self.flowerNames, overlarge=self.overlarge, precomputedAlignmentIDs = self.precomputedAlignmentIDs))
        
    def makeChildJobs(self, flowersAndSizes, job, overlargeJob=None, 
                         phaseNode=None, runFlowerStats=False):
        """Make a set of child jobs for a given set of flowers and chosen child job
        """
        if overlargeJob == None:
            overlargeJob = job
        if phaseNode == None:
            phaseNode = self.phaseNode
        for overlarge, flowerNames in flowersAndSizes:
            if overlarge: #Make sure large flowers are on their own, in their own job
                if runFlowerStats:
                    flowerStatsString = runCactusFlowerStats(cactusDiskDatabaseString=self.cactusDiskDatabaseString, flowerName=decodeFirstFlowerName(flowerNames))
                    logger.info("Adding an oversize flower for job class %s and stats %s" \
                                             % (overlargeJob, flowerStatsString))
                else:
                    logger.info("Adding an oversize flower %s for job class %s" \
                                             % (decodeFirstFlowerName(flowerNames), overlargeJob))
                self.addChild(overlargeJob(cactusDiskDatabaseString=self.cactusDiskDatabaseString, phaseNode=phaseNode, 
                                                    constantsNode=self.constantsNode,
                                                    flowerNames=flowerNames, overlarge=True)) #This ensures overlarge flowers, 
            else:
                self.addChild(job(cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                                           phaseNode=phaseNode, constantsNode=self.constantsNode, flowerNames=flowerNames, overlarge=False))
        
    def makeRecursiveJobs(self, job=None, phaseNode=None, runFlowerStats=False):
        """Make a set of child jobs for a given set of parent flowers.
        """
        if job == None:
            job = self.__class__
        jobNode = getJobNode(self.phaseNode, job)
        flowersAndSizes=runCactusGetFlowers(cactusDiskDatabaseString=self.cactusDiskDatabaseString, flowerNames=self.flowerNames, 
                                            minSequenceSizeOfFlower=getOptionalAttrib(jobNode, "minFlowerSize", int, 0), 
                                            maxSequenceSizeOfFlowerGrouping=getOptionalAttrib(jobNode, "maxFlowerGroupSize", int, 
                                            default=CactusRecursionJob.maxSequenceSizeOfFlowerGroupingDefault),
                                            maxSequenceSizeOfSecondaryFlowerGrouping=getOptionalAttrib(jobNode, "maxFlowerWrapperGroupSize", int, 
                                            default=CactusRecursionJob.maxSequenceSizeOfFlowerGroupingDefault))
        self.makeChildJobs(flowersAndSizes=flowersAndSizes, 
                              job=job, phaseNode=phaseNode,
                              runFlowerStats=runFlowerStats)
    
    def makeExtendingJobs(self, job, overlargeJob=None, phaseNode=None, runFlowerStats=False):
        """Make set of child jobs that extend the current cactus tree.
        """
        jobNode = getJobNode(self.phaseNode, job)
        flowersAndSizes=runCactusExtendFlowers(cactusDiskDatabaseString=self.cactusDiskDatabaseString, flowerNames=self.flowerNames, 
                                              minSequenceSizeOfFlower=getOptionalAttrib(jobNode, "minFlowerSize", int, 1), 
                                              maxSequenceSizeOfFlowerGrouping=getOptionalAttrib(jobNode, "maxFlowerGroupSize", int, 
                                              default=CactusRecursionJob.maxSequenceSizeOfFlowerGroupingDefault))
        self.makeChildJobs(flowersAndSizes=flowersAndSizes, 
                              job=job, overlargeJob=overlargeJob, 
                              phaseNode=phaseNode,
                              runFlowerStats=runFlowerStats)
    
    def makeWrapperJobs(self, job, overlargeJob=None, phaseNode=None, runFlowerStats=False):
        """Takes the list of flowers for a recursive job and splits them up to fit the given wrapper job(s).
        """
        self.makeChildJobs(flowersAndSizes=runCactusSplitFlowersBySecondaryGrouping(self.flowerNames), 
                              job=job, overlargeJob=overlargeJob, phaseNode=phaseNode, runFlowerStats=runFlowerStats)

############################################################
############################################################
############################################################
##The (optional) blast phase that uses the trimming strategy.
############################################################
############################################################
############################################################

def prependUniqueIDs(fas, outputDir):
    """Prepend unique ints to fasta headers.

    (prepend rather than append since trimmed outgroups have a start
    token appended, which complicates removal slightly)
    """
    uniqueID = 0
    ret = []
    for fa in fas:
        outPath = os.path.join(outputDir, os.path.basename(fa))
        out = open(outPath, 'w')
        for line in open(fa):
            if len(line) > 0 and line[0] == '>':
                tokens = line[1:].split()
                tokens[0] = "id=%d|%s" % (uniqueID, tokens[0])
                out.write(">%s\n" % "".join(tokens))
            else:
                out.write(line)
        ret.append(outPath)
        uniqueID += 1
    return ret

def setupDivergenceArgs(cactusWorkflowArguments):
    #Adapt the config file to use arguments for the appropriate divergence distance
    cactusWorkflowArguments.longestPath = getLongestPath(newickTreeParser(cactusWorkflowArguments.speciesTree))
    if cactusWorkflowArguments.outgroupEventNames == None:
        distanceToAddToRootAlignment = getOptionalAttrib(cactusWorkflowArguments.configNode, "distanceToAddToRootAlignment", float, 0.0)
        cactusWorkflowArguments.longestPath += distanceToAddToRootAlignment
    cw = ConfigWrapper(cactusWorkflowArguments.configNode)
    cw.substituteAllDivergenceContolledParametersWithLiterals(cactusWorkflowArguments.longestPath)

def setupFilteringByIdentity(cactusWorkflowArguments):
    #Filter by identity
    cafNode = findRequiredNode(cactusWorkflowArguments.configNode, "caf")
    if getOptionalAttrib(cafNode, "filterByIdentity", bool, False): #Do the identity filtering
        adjustedPath = max(float(cafNode.attrib["identityRatio"]) * cactusWorkflowArguments.longestPath,
        float(cafNode.attrib["minimumDistance"]))
        identity = str(100 - math.ceil(100 * inverseJukesCantor(adjustedPath)))
        cafNode.attrib["lastzArguments"] = cafNode.attrib["lastzArguments"] + (" --identity=%s" % identity)


class CactusTrimmingBlastPhase(CactusPhasesJob):
    """Blast ingroups vs outgroups using the trimming strategy before
    running cactus setup.
    """
    def run(self, fileStore):
        # Not worth doing extra work if there aren't any outgroups
        assert self.cactusWorkflowArguments.outgroupEventNames is not None

        fileStore.logToMaster("Running blast using the trimming strategy")

        outgroupsDir = os.path.join(fileStore.getLocalTempDir(), "outgroupFragments/")
        os.mkdir(outgroupsDir)

        # Get ingroup and outgroup sequences
        exp = ExperimentWrapper(self.cactusWorkflowArguments.experimentNode)
        exp.setSequences([fileStore.readGlobalFile(seqID) for seqID in exp.getSequenceIDs()])
        seqMap = exp.buildSequenceMap()
        sequences = exp.getSequences()

        # Prepend unique ID to fasta headers to prevent name collision
        renamedInputSeqDir = os.path.join(fileStore.getLocalTempDir(), "renamedInputs")
        os.mkdir(renamedInputSeqDir)
        uniqueFas = prependUniqueIDs(sequences, renamedInputSeqDir)
        uniqueFaIDs = [fileStore.writeGlobalFile(seq, cleanup=False) for seq in uniqueFas]
        exp.setSequenceIDs(uniqueFaIDs)
        self.phaseNode.attrib["seqIDs"] = " ".join(uniqueFaIDs)
        
        seqIDMap = dict(zip(seqMap.keys(), uniqueFaIDs))
        ingroupIDs = map(lambda x: x[1], filter(lambda x: x[0] not in exp.getOutgroupEvents(), seqIDMap.items()))
        outgroupIDs = [seqIDMap[i] for i in exp.getOutgroupEvents()]
        fileStore.logToMaster("Ingroup sequences: %s" % (ingroupIDs))
        fileStore.logToMaster("Outgroup sequences: %s" % (outgroupIDs))

        # Change the blast arguments depending on the divergence
        setupDivergenceArgs(self.cactusWorkflowArguments)
        setupFilteringByIdentity(self.cactusWorkflowArguments)

        alignmentsFile = getTempFile("unconvertedAlignments", rootDir=fileStore.getLocalTempDir())
        # FIXME: this is really ugly and steals the options from the caf tag
        trimBlastResults = self.addChild(BlastIngroupsAndOutgroups(
                                          BlastOptions(chunkSize=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "chunkSize", int),
                                                        overlapSize=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "overlapSize", int),
                                                        lastzArguments=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "lastzArguments"),
                                                        compressFiles=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "compressFiles", bool),
                                                        realign=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "realign", bool), 
                                                        realignArguments=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "realignArguments"),
                                                        memory=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "lastzMemory", int, sys.maxint),
                                                        minimumSequenceLength=getOptionalAttrib(findRequiredNode(self.cactusWorkflowArguments.configNode, "caf"), "minimumSequenceLengthForBlast", int, 1),
                                                       trimFlanking=self.getOptionalPhaseAttrib("trimFlanking", int, 10),
                                                       trimMinSize=self.getOptionalPhaseAttrib("trimMinSize", int, 0),
                                                       trimThreshold=self.getOptionalPhaseAttrib("trimThreshold", float, 0.8),
                                                       trimWindowSize=self.getOptionalPhaseAttrib("trimWindowSize", int, 10),
                                                       trimOutgroupFlanking=self.getOptionalPhaseAttrib("trimOutgroupFlanking", int, 100)), ingroupIDs, outgroupIDs)).rv()
        self.phaseNode.attrib["trimBlastResults"] = trimBlastResults

        return self.makeFollowOnPhaseJob(CactusTrimmingBlastPhase2, phaseName="trimBlast")
class CactusTrimmingBlastPhase2(CactusPhasesJob):
    def run(self, fileStore):
        exp = ExperimentWrapper(self.cactusWorkflowArguments.experimentNode)
        seqIDs = self.phaseNode.attrib["seqIDs"].split()
        seqMap = exp.buildSequenceMap()
        seqIDMap = dict(zip(seqMap.keys(), seqIDs))
        trimBlastResults = self.phaseNode.attrib["trimBlastResults"]
        
        outgroupFragmentIDs = trimBlastResults["outgroupFragmentIDs"]
        alignmentsID = trimBlastResults["alignmentsID"]

        self.phaseNode.attrib["aignmentsID"] = alignmentsID
        # Point the outgroup sequences to their trimmed versions for
        # phases after this one.
        for i, outgroup in enumerate(exp.getOutgroupEvents()):
            seqIDMap[outgroup] = outgroupFragmentIDs[i]
        self.phaseNode.attrib["seqIDs"] = " ".join(seqIDMap.values())

        return self.makeFollowOnPhaseJob(CactusSetupPhase, "setup")
        
############################################################
############################################################
############################################################
##The setup phase.
############################################################
############################################################
############################################################

def getLongestPath(node, distance=0.0):

    """Identify the longest path from the mrca of the leaves of the species tree.
    """
    i, j = distance, distance
    if node.left != None:
        i = getLongestPath(node.left, abs(node.left.distance)) + distance
    if node.right != None:  
        j = getLongestPath(node.right, abs(node.right.distance)) + distance
    return max(i, j)

class CactusSetupPhase(CactusPhasesJob):
    """Initialises the cactus database and adapts the config file for the run.
    """
    def run(self, fileStore):
        cw = ConfigWrapper(self.cactusWorkflowArguments.configNode)

        if (not self.cactusWorkflowArguments.configWrapper.getDoTrimStrategy()) or (self.cactusWorkflowArguments.outgroupEventNames == None):
            setupDivergenceArgs(self.cactusWorkflowArguments)

        # we circumvent makeFollowOnPhaseJob() interface for this job.
        setupJob = CactusSetupPhase2(cactusWorkflowArguments=self.cactusWorkflowArguments,
                                       phaseName='setup', topFlowerName=self.topFlowerName,
                                       index=0)
        if "seqIDs" in self.phaseNode.attrib:
            setupJob.phaseNode.attrib["seqIDs"] = self.phaseNode.attrib["seqIDs"]
        
        #Get the db running and the actual setup going.
        exp = ExperimentWrapper(self.cactusWorkflowArguments.experimentNode)
        if exp.getDbType() == "kyoto_tycoon":
            logger.info("Created ktserver pattern job cactus_setup")
            memory = cw.getKtserverMemory(default=getOptionalAttrib(
                    self.constantsNode, "defaultMemory", int, default=sys.maxint))
            cores = cw.getKtserverCpu(default=getOptionalAttrib(
                    self.constantsNode, "defaultCpu", int, default=sys.maxint))
            dbElem = ExperimentWrapper(self.cactusWorkflowArguments.experimentNode)
            dbString = self.addService(KtServerService(dbElem = dbElem, isSecondary=False, memory = memory, cores = cores))
            setupJob.cactusWorkflowArguments.cactusDiskDatabaseString = dbString
            results = self.addChild(setupJob).rv()
            logger.info("Pickled setup job")
            return results
        else:
            logger.info("Created follow-on job cactus_setup")
            return self.addFollowOn(setupJob).rv()
        
class CactusSetupPhase2(CactusPhasesJob):   
    def run(self, fileStore):        
        #Now run setup
        if "seqIDs" in self.phaseNode.attrib:
            logger.info("Using sequences from file Store.")
            sequenceIDs = self.phaseNode.attrib["seqIDs"].split()
            sequences = [fileStore.readGlobalFile(fileID) for fileID in sequenceIDs]
        else:
            logger.info("Reading sequences from permanent input paths.")
            sequenceIDs = ExperimentWrapper(self.cactusWorkflowArguments.experimentNode).getSequenceIDs()
            sequences = [fileStore.readGlobalFile(seqID) for seqID in sequenceIDs]
        messages = runCactusSetup(cactusDiskDatabaseString=self.cactusWorkflowArguments.cactusDiskDatabaseString, 
                       sequences=sequences,
                       newickTreeString=self.cactusWorkflowArguments.speciesTree, 
                       outgroupEvents=self.cactusWorkflowArguments.outgroupEventNames,
                       makeEventHeadersAlphaNumeric=self.getOptionalPhaseAttrib("makeEventHeadersAlphaNumeric", bool, False))
        for message in messages:
            fileStore.logToMaster(message)
        return self.makeFollowOnPhaseJob(CactusCafPhase, "caf")
        
############################################################
############################################################
############################################################
#The CAF phase.
#
#Creates the reconstruction structure with blocks
############################################################
############################################################
############################################################

def inverseJukesCantor(d):
    """Takes a substitution distance and calculates the number of expected changes per site (inverse jukes cantor)
    d = -3/4 * log(1 - 4/3 * p)
    exp(-4/3 * d) = 1 - 4/3 * p
    4/3 * p = 1 - exp(-4/3 * d)
    p = 3/4 * (1 - exp(-4/3 * d))
    """
    assert d >= 0.0
    return 0.75 * (1 - math.exp(-d * 4.0/3.0))
    
class CactusCafPhase(CactusPhasesJob):      
    def run(self, fileStore):
        if (not self.cactusWorkflowArguments.configWrapper.getDoTrimStrategy()) or (self.cactusWorkflowArguments.outgroupEventNames == None):
            setupFilteringByIdentity(self.cactusWorkflowArguments)
        #Setup any constraints
        if self.getPhaseIndex() == 0 and self.cactusWorkflowArguments.constraintsFile != None: #Setup the constraints arg
            newConstraintsFile = fileStore.getLocalTempFile()
            runCactusConvertAlignmentToCactus(self.cactusWorkflowArguments.cactusDiskDatabaseString,
                                              self.cactusWorkflowArguments.constraintsFile, newConstraintsFile)
            self.phaseNode.attrib["constraintsID"] = fileStore.writeGlobalFile(newConstraintsFile, cleanup=False)
        if "alignmentsID" in self.phaseNode.attrib:
            # An alignment file has been provided (likely from the
            # ingroup vs. outgroup blast stage), so just run caf using
            # that file
            assert self.getPhaseNumber() == 1
            convertedAlignmentsFile = fileStore.getLocalTempFile()
            # Convert the cigar file to use 64-bit cactus Names instead of the headers.
            runConvertAlignmentsToInternalNames(self.cactusWorkflowArguments.cactusDiskDatabaseString, self.cactusWorkflowArguments.alignmentsID, convertedAlignmentsFile, self.topFlowerName)
            fileStore.logToMaster("Converted headers of cigar file %s to internal names, new file %s" % (self.cactusWorkflowArguments.alignmentsID, convertedAlignmentsFile))
            self.cactusWorkflowArguments.alignmentsID = fileStore.writeGlobalFile(convertedAlignmentsFile, cleanup=False)
            # While we're at it, remove the unique IDs prepended to
            # the headers inside the cactus DB.
            runStripUniqueIDs(self.cactusWorkflowArguments.cactusDiskDatabaseString)
            return self.runPhase(CactusCafWrapperLarge2, CactusBarPhase, "bar")
        elif self.getPhaseIndex()+1 < self.getPhaseNumber(): #Check if there is a repeat phase
            return self.runPhase(CactusCafRecursion, CactusCafPhase, "caf", index=self.getPhaseIndex()+1)
        else:
            return self.runPhase(CactusCafRecursion, CactusBarPhase, "bar")

class CactusCafRecursion(CactusRecursionJob):
    """This job does the get flowers down pass for the CAF alignment phase.
    """    
    def run(self, fileStore):
        self.makeRecursiveJobs()
        self.makeExtendingJobs(job=CactusCafWrapper, overlargeJob=CactusCafWrapperLarge, runFlowerStats=True)
        
class CactusCafWrapper(CactusRecursionJob):
    """Runs cactus_core upon a set of flowers and no alignment file.
    """
    def runCactusCafInWorkflow(self, alignmentFile, constraints):
        messages = runCactusCaf(cactusDiskDatabaseString=self.cactusDiskDatabaseString,
                          alignments=alignmentFile, 
                          flowerNames=self.flowerNames,
                          constraints=constraints,  
                          annealingRounds=self.getOptionalPhaseAttrib("annealingRounds"),  
                          deannealingRounds=self.getOptionalPhaseAttrib("deannealingRounds"),
                          trim=self.getOptionalPhaseAttrib("trim"),
                          minimumTreeCoverage=self.getOptionalPhaseAttrib("minimumTreeCoverage", float),
                          blockTrim=self.getOptionalPhaseAttrib("blockTrim", float),
                          minimumBlockDegree=self.getOptionalPhaseAttrib("minimumBlockDegree", int), 
                          minimumIngroupDegree=self.getOptionalPhaseAttrib("minimumIngroupDegree", int),
                          minimumOutgroupDegree=self.getOptionalPhaseAttrib("minimumOutgroupDegree", int),
                          singleCopyIngroup=self.getOptionalPhaseAttrib("singleCopyIngroup", bool),
                          singleCopyOutgroup=self.getOptionalPhaseAttrib("singleCopyOutgroup"),
                          lastzArguments=self.getOptionalPhaseAttrib("lastzArguments"),
                          minimumSequenceLengthForBlast=self.getOptionalPhaseAttrib("minimumSequenceLengthForBlast", int, 1),
                          maxAdjacencyComponentSizeRatio=self.getOptionalPhaseAttrib("maxAdjacencyComponentSizeRatio", float),
                          minLengthForChromosome=self.getOptionalPhaseAttrib("minLengthForChromosome", int),
                          proportionOfUnalignedBasesForNewChromosome=self.getOptionalPhaseAttrib("proportionOfUnalignedBasesForNewChromosome", float),
                          maximumMedianSequenceLengthBetweenLinkedEnds=self.getOptionalPhaseAttrib("maximumMedianSequenceLengthBetweenLinkedEnds", int),
                          realign=self.getOptionalPhaseAttrib("realign", bool),
                          realignArguments=self.getOptionalPhaseAttrib("realignArguments"),
                          removeRecoverableChains=self.getOptionalPhaseAttrib("removeRecoverableChains"),
                          minimumNumberOfSpecies=self.getOptionalPhaseAttrib("minimumNumberOfSpecies", int))
        for message in messages:
            fileStore.logToMaster(message)
    
    def run(self, fileStore):
        constraints = None
        if "constraintsID" in self.phaseNode.attrib:
            constraints = fileStore.readGlobalFile(self.getOptionalPhaseAttrib("constraintsID"))
        self.runCactusCafInWorkflow(alignmentFile=None, constraints=constraints)
       
class CactusCafWrapperLarge(CactusRecursionJob):
    """Runs blast on the given flower and passes the resulting alignment to cactus core.
    """
    def run(self, fileStore):
        logger.info("Starting the cactus aligner job")
        #Generate a temporary file to hold the alignments
        flowerName = decodeFirstFlowerName(self.flowerNames)
        alignmentsID = self.addChild(BlastFlower(cactusDisk=self.cactusDiskDatabaseString, 
                                          flowerName=flowerName, 
                                          blastOptions=\
                                          BlastOptions(chunkSize=self.getOptionalPhaseAttrib("chunkSize", int),
                                                        overlapSize=self.getOptionalPhaseAttrib("overlapSize", int),
                                                        lastzArguments=self.getOptionalPhaseAttrib("lastzArguments"),
                                                        compressFiles=self.getOptionalPhaseAttrib("compressFiles", bool),
                                                        realign=self.getOptionalPhaseAttrib("realign", bool), 
                                                        realignArguments=self.getOptionalPhaseAttrib("realignArguments"),
                                                        memory=self.getOptionalPhaseAttrib("lastzMemory", int, sys.maxint),
                                                        minimumSequenceLength=self.getOptionalPhaseAttrib("minimumSequenceLengthForBlast", int, 1)))).rv()
        #Now setup a call to cactus core wrapper as a follow on
        self.phaseNode.attrib["alignmentIDs"] = alignmentsID
        self.makeFollowOnRecursiveJob(CactusCafWrapperLarge2)
        
class CactusCafWrapperLarge2(CactusCafWrapper):
    """Runs cactus_core upon a one flower and one alignment file.
    """
    def run(self, fileStore):
        alignments = None
        if "alignmentsID" in self.phaseNode.attrib:
            alignments = fileStore.readGlobalFile(self.phaseNode.attrib["alignmentsID"])
        constraints = None
        if "constraintsID" in self.phaseNode.attrib:
            constraints = fileStore.readGlobalFile(self.phaseNode.attrib["constraintsID"])
        logger.info("Alignments file: %s" % alignments)
        self.runCactusCafInWorkflow(alignmentFile=alignments, constraints=constraints)
        
############################################################
############################################################
############################################################
#The BAR phase.
#
#Creates the reconstruction structure with blocks
############################################################
############################################################
############################################################

class CactusBarPhase(CactusPhasesJob): 
    """Runs bar algorithm
    """  
    def run(self, fileStore):
        return self.runPhase(CactusBarRecursion, CactusNormalPhase, "normal", doRecursion=self.getOptionalPhaseAttrib("runBar", bool, False))

class CactusBarRecursion(CactusRecursionJob):
    """This job does the get flowers down pass for the BAR alignment phase.
    """
    def run(self, fileStore):
        self.makeRecursiveJobs()
        self.makeExtendingJobs(job=CactusBarWrapper, overlargeJob=CactusBarWrapperLarge, runFlowerStats=True)

def runBarForJob(self, calculateWhichEndsToComputeSeparately=None, endAlignmentsToPrecomputeOutputFile=None, precomputedAlignments=None):
    return runCactusBar(cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                 flowerNames=self.flowerNames, 
                 maximumLength=self.getOptionalPhaseAttrib("bandingLimit", float),
                 spanningTrees=self.getOptionalPhaseAttrib("spanningTrees", int), 
                 gapGamma=self.getOptionalPhaseAttrib( "gapGamma", float), 
                 splitMatrixBiggerThanThis=self.getOptionalPhaseAttrib("splitMatrixBiggerThanThis", int), 
                 anchorMatrixBiggerThanThis=self.getOptionalPhaseAttrib("anchorMatrixBiggerThanThis", int), 
                 repeatMaskMatrixBiggerThanThis=self.getOptionalPhaseAttrib("repeatMaskMatrixBiggerThanThis", int), 
                 diagonalExpansion=self.getOptionalPhaseAttrib("diagonalExpansion"),
                 constraintDiagonalTrim=self.getOptionalPhaseAttrib("constraintDiagonalTrim", int), 
                 minimumBlockDegree=self.getOptionalPhaseAttrib("minimumBlockDegree", int),
                 minimumIngroupDegree=self.getOptionalPhaseAttrib("minimumIngroupDegree", int),
                 minimumOutgroupDegree=self.getOptionalPhaseAttrib("minimumOutgroupDegree", int),
                 alignAmbiguityCharacters=self.getOptionalPhaseAttrib("alignAmbiguityCharacters", bool),
                 pruneOutStubAlignments=self.getOptionalPhaseAttrib("pruneOutStubAlignments", bool),
                 maximumNumberOfSequencesBeforeSwitchingToFast=self.getOptionalPhaseAttrib("maximumNumberOfSequencesBeforeSwitchingToFast", int),
                 calculateWhichEndsToComputeSeparately=calculateWhichEndsToComputeSeparately,
                 endAlignmentsToPrecomputeOutputFile=endAlignmentsToPrecomputeOutputFile,
                 largeEndSize=self.getOptionalPhaseAttrib("largeEndSize", int),
                 precomputedAlignments=precomputedAlignments,
                 minimumNumberOfSpecies=self.getOptionalPhaseAttrib("minimumNumberOfSpecies", int))

class CactusBarWrapper(CactusRecursionJob):
    """Runs the BAR algorithm implementation.
    """
    def run(self, fileStore):
        messages = runBarForJob(self)
        for message in messages:
            fileStore.logToMaster(message)       
        
class CactusBarWrapperLarge(CactusRecursionJob):
    """Breaks up the bar into a series of smaller bars, then runs them.
    """
    def run(self, fileStore):
        logger.info("Starting the cactus bar preprocessor job to breakup the bar alignment")
        precomputedAlignmentFiles = []
        veryLargeEndSize=self.getOptionalPhaseAttrib("veryLargeEndSize", int, default=1000000)
        maxFlowerGroupSize = self.getOptionalJobAttrib("maxFlowerGroupSize", int, 
                                            default=CactusRecursionJob.maxSequenceSizeOfFlowerGroupingDefault)
        endsToAlign = []
        totalSize = 0
        precomputedAlignmentIDs = []
        for line in runBarForJob(self, calculateWhichEndsToComputeSeparately=True):
            endToAlign, sequencesInEndAlignment, basesInEndAlignment = line.split()
            sequencesInEndAlignment = int(sequencesInEndAlignment)
            basesInEndAlignment = int(basesInEndAlignment)
            #If we have a really big end align separately
            if basesInEndAlignment >= veryLargeEndSize:
                alignmentID = self.addChild(CactusBarEndAlignerWrapper(self.phaseNode, self.constantsNode,
                                                         self.cactusDiskDatabaseString, self.flowerNames,
                                                         True, [ endToAlign ])).rv()
                precomputedAlignmentIDs.append(alignmentID)
                fileStore.logToMaster("Precomputing very large end alignment for %s with %i caps and %i bases" % \
                             (endToAlign, sequencesInEndAlignment, basesInEndAlignment))
            else:
                endsToAlign.append(endToAlign)
                totalSize += basesInEndAlignment
                if totalSize >= maxFlowerGroupSize:
                    alignmentID = self.addChild(CactusBarEndAlignerWrapper(self.phaseNode, self.constantsNode, self.cactusDiskDatabaseString, self.flowerNames, False, endsToAlign)).rv()
                    precomputedAlignmentIDs.append(alignmentID)
                    endsToAlign = []
                    totalSize = 0
        if len(endsToAlign) > 0:
            precomputedAlignmentIDs.append(self.addChild(CactusBarEndAlignerWrapper(self.phaseNode, self.constantsNode, self.cactusDiskDatabaseString, self.flowerNames, False, endsToAlign)).rv())
        self.precomputedAlignmentIDs = precomputedAlignmentIDs
        self.makeFollowOnRecursiveJob(CactusBarWrapperWithPrecomputedEndAlignments)
        fileStore.logToMaster("Breaking bar job into %i separate jobs" % \
                             (len(precomputedAlignmentIDs)))
        
class CactusBarEndAlignerWrapper(CactusRecursionJob):
    """Computes an end alignment.
    """
    def __init__(self, phaseNode, constantsNode, cactusDiskDatabaseString, flowerNames, overlarge, endsToAlign):
        CactusRecursionJob.__init__(self, phaseNode, constantsNode, cactusDiskDatabaseString, flowerNames, overlarge)
        self.endsToAlign = endsToAlign
    
    def run(self, fileStore):
        self.endsToAlign = [ int(i) for i in self.endsToAlign ]
        self.endsToAlign.sort()
        self.flowerNames = encodeFlowerNames((decodeFirstFlowerName(self.flowerNames),) + tuple(self.endsToAlign)) #The ends to align become like extra flower names
        alignmentFile = fileStore.getLocalTempFile()
        messages = runBarForJob(self, 
                                   endAlignmentsToPrecomputeOutputFile=alignmentFile)
        for message in messages:
            fileStore.logToMaster(message)
        return fileStore.writeGlobalFile(alignmentFile, cleanup=False)
        
class CactusBarWrapperWithPrecomputedEndAlignments(CactusRecursionJob):
    """Runs the BAR algorithm implementation with some precomputed end alignments.
    """
    def run(self, fileStore):
        if self.precomputedAlignmentIDs:
            precomputedAlignments = [fileStore.readGlobalFile(fileID) for fileID in self.precomputedAlignmentIDs]
            messages = runBarForJob(self, precomputedAlignments=" ".join(precomputedAlignments))
            map(fileStore.deleteGlobalFile, self.precomputedAlignmentIDs)
        else:
            messages = runBarForJob(self)
        for message in messages:
            fileStore.logToMaster(message)
        
############################################################
############################################################
############################################################
#Normalisation pass
############################################################
############################################################
############################################################
    
class CactusNormalPhase(CactusPhasesJob):
    """Phase to normalise the graph, ensuring all chains are maximal
    """
    def run(self, fileStore):
        normalisationIterations = self.getOptionalPhaseAttrib("iterations", int, default=0)
        if normalisationIterations > 0:
            self.phaseNode.attrib["normalised"] = "1"
            self.phaseNode.attrib["iterations"] = str(normalisationIterations-1)
            return self.runPhase(CactusNormalRecursion, CactusNormalPhase, "normal")
        else:
            return self.makeFollowOnPhaseJob(CactusAVGPhase, "avg")
     
class CactusNormalRecursion(CactusRecursionJob):
    """This job does the down pass for the normal phase.
    """
    def run(self, fileStore):
        self.makeRecursiveJobs()
        self.makeFollowOnRecursiveJob(CactusNormalRecursion2)
        
class CactusNormalRecursion2(CactusRecursionJob):
    """This job sets up the normal wrapper in an up traversal of the tree.
    """
    def run(self, fileStore):
        self.makeWrapperJobs(CactusNormalWrapper)
        
class CactusNormalWrapper(CactusRecursionJob):
    """This jobs run the normalisation script.
    """ 
    def run(self, fileStore):
        runCactusMakeNormal(self.cactusDiskDatabaseString, flowerNames=self.flowerNames, 
                            maxNumberOfChains=self.getOptionalPhaseAttrib("maxNumberOfChains", int, default=30))

############################################################
############################################################
############################################################
#Phylogeny pass
############################################################
############################################################
############################################################
    
class CactusAVGPhase(CactusPhasesJob): 
    """Phase to build avgs for each flower.
    """       
    def run(self, fileStore):
        return self.runPhase(CactusAVGRecursion, CactusReferencePhase, "reference", doRecursion=self.getOptionalPhaseAttrib("buildAvgs", bool, False))

class CactusAVGRecursion(CactusRecursionJob):
    """This job does the recursive pass for the AVG phase.
    """
    def run(self, fileStore):
        self.makeFollowOnRecursiveJob(CactusAVGRecursion2)
        self.makeWrapperJobs(CactusAVGWrapper)

class CactusAVGRecursion2(CactusRecursionJob):
    """This job does the recursive pass for the AVG phase.
    """
    def run(self, fileStore):
        self.makeRecursiveJobs(job=CactusAVGRecursion)

class CactusAVGWrapper(CactusRecursionJob):
    """This job runs tree building
    """
    def run(self, fileStore):
        runCactusPhylogeny(self.cactusDiskDatabaseString, flowerNames=self.flowerNames)

############################################################
############################################################
############################################################
#Reference pass
############################################################
############################################################
############################################################
    
class CactusReferencePhase(CactusPhasesJob):
    def run(self, fileStore):
        """Runs the reference problem algorithm
        """
        self.setupSecondaryDatabase()
        self.phaseNode.attrib["experimentPath"] = self.cactusWorkflowArguments.experimentFile
        self.phaseNode.attrib["secondaryDatabaseString"] = self.cactusWorkflowArguments.secondaryDatabaseString
        return self.runPhase(CactusReferenceRecursion, CactusSetReferenceCoordinatesDownPhase, "reference", 
                      doRecursion=self.getOptionalPhaseAttrib("buildReference", bool, False),
                      launchSecondaryKtForRecursiveJob = True)
        
class CactusReferenceRecursion(CactusRecursionJob):
    """This job creates the wrappers to run the reference problem algorithm, the follow on job then recurses down.
    """
    def run(self, fileStore):
        self.makeWrapperJobs(CactusReferenceWrapper, runFlowerStats=True)
        self.makeFollowOnRecursiveJob(CactusReferenceRecursion2)
        
class CactusReferenceWrapper(CactusRecursionJob):
    """Actually run the reference code.
    """
    def run(self, fileStore):
        runCactusReference(cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                       flowerNames=self.flowerNames, 
                       matchingAlgorithm=self.getOptionalPhaseAttrib("matchingAlgorithm"), 
                       permutations=self.getOptionalPhaseAttrib("permutations", int),
                       referenceEventString=self.getOptionalPhaseAttrib("reference"), 
                       useSimulatedAnnealing=self.getOptionalPhaseAttrib("useSimulatedAnnealing", bool),
                       theta=self.getOptionalPhaseAttrib("theta", float),
                       maxWalkForCalculatingZ=self.getOptionalPhaseAttrib("maxWalkForCalculatingZ", int),
                       ignoreUnalignedGaps=self.getOptionalPhaseAttrib("ignoreUnalignedGaps", bool),
                       wiggle=self.getOptionalPhaseAttrib("wiggle", float),
                       numberOfNs=self.getOptionalPhaseAttrib("numberOfNs", int),
                       minNumberOfSequencesToSupportAdjacency=self.getOptionalPhaseAttrib("minNumberOfSequencesToSupportAdjacency", int),
                       makeScaffolds=self.getOptionalPhaseAttrib("makeScaffolds", bool))

class CactusReferenceRecursion2(CactusRecursionJob):
    def run(self, fileStore):
        self.makeRecursiveJobs(job=CactusReferenceRecursion)
        self.makeFollowOnRecursiveJob(CactusReferenceRecursion3)
        
class CactusReferenceRecursion3(CactusRecursionJob):
    """After completing the recursion for the reference algorithm, the up pass of adding in the reference coordinates is performed.
    """
    def run(self, fileStore):
        self.makeWrapperJobs(CactusSetReferenceCoordinatesUpWrapper)

class CactusSetReferenceCoordinatesUpWrapper(CactusRecursionJob):
    """Does the up pass for filling in the reference sequence coordinates, once a reference has been established.
    """ 
    def run(self, fileStore):
        runCactusAddReferenceCoordinates(cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                                         secondaryDatabaseString=self.getOptionalPhaseAttrib("secondaryDatabaseString"),
                                         flowerNames=self.flowerNames,
                                         referenceEventString=self.getOptionalPhaseAttrib("reference"),
                                         outgroupEventString=self.getOptionalPhaseAttrib("outgroup"),
                                         bottomUpPhase=True)
        
class CactusSetReferenceCoordinatesDownPhase(CactusPhasesJob):
    """This is the second part of the reference coordinate setting, the down pass.
    """
    def run(self, fileStore):
        self.cleanupSecondaryDatabase()
        self.runPhase(CactusSetReferenceCoordinatesDownRecursion, CactusExtractReferencePhase, "check", doRecursion=self.getOptionalPhaseAttrib("buildReference", bool, False))
        
class CactusSetReferenceCoordinatesDownRecursion(CactusRecursionJob):
    """Does the down pass for filling Fills in the coordinates, once a reference is added.
    """        
    def run(self, fileStore):
        self.makeWrapperJobs(CactusSetReferenceCoordinatesDownWrapper)
        self.makeFollowOnRecursiveJob(CactusSetReferenceCoordinatesDownRecursion2)

class CactusSetReferenceCoordinatesDownRecursion2(CactusRecursionJob):
    def run(self, fileStore):
        self.makeRecursiveJobs(job=CactusSetReferenceCoordinatesDownRecursion)
        
class CactusSetReferenceCoordinatesDownWrapper(CactusRecursionJob):
    """Does the down pass for filling Fills in the coordinates, once a reference is added.
    """        
    def run(self, fileStore):
        runCactusAddReferenceCoordinates(cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                                         flowerNames=self.flowerNames,
                                         referenceEventString=self.getOptionalPhaseAttrib("reference"),
                                         outgroupEventString=self.getOptionalPhaseAttrib("outgroup"), 
                                         bottomUpPhase=False)

class CactusExtractReferencePhase(CactusPhasesJob):
    def run(self, fileStore):
        if hasattr(self.cactusWorkflowArguments, 'buildReference') and\
               self.cactusWorkflowArguments.buildReference:
            fileStore.logToMaster("Starting Reference Extract Phase")
            experiment = ExperimentWrapper(self.cactusWorkflowArguments.experimentNode)
            if experiment.getReferencePath() is not None:
                eventName = os.path.basename(experiment.getReferencePath())
                if eventName.find('.') >= 0:
                    eventName = eventName[:eventName.rfind('.')]
                    referencePath = os.path.join(fileStore.getLocalTempDir(), "tempReference")
                    cmdLine = "cactus_getReferenceSeq --cactusDisk \'%s\' --flowerName 0 --referenceEventString %s --outputFile %s --logLevel %s" % \
                              (self.cactusWorkflowArguments.cactusDiskDatabaseString, eventName,
                               referencePath, getLogLevelString())                        
                    system(cmdLine)
                    experiment.setReferenceID(fileStore.writeGlobalFile(referencePath))
        self.makeFollowOnPhaseJob(CactusCheckPhase, "check")

############################################################
############################################################
############################################################
#Check pass
############################################################
############################################################
############################################################
    
class CactusCheckPhase(CactusPhasesJob):
    """The check phase, where we verify everything is as it should be
    """
    def run(self, fileStore):
        normalNode = findRequiredNode(self.cactusWorkflowArguments.configNode, "normal")
        self.phaseNode.attrib["checkNormalised"] = getOptionalAttrib(normalNode, "normalised", default="0")
        self.runPhase(CactusCheckRecursion, CactusHalGeneratorPhase, "hal", doRecursion=self.getOptionalPhaseAttrib("runCheck", bool, False))
        
class CactusCheckRecursion(CactusRecursionJob):
    """This job does the recursive pass for the check phase.
    """
    def run(self, fileStore):
        self.makeRecursiveJobs()
        self.makeWrapperJobs(CactusCheckWrapper)
        
class CactusCheckWrapper(CactusRecursionJob):
    """Runs the actual check wrapper
    """
    def run(self, fileStore):
        runCactusCheck(self.cactusDiskDatabaseString, self.flowerNames, checkNormalised=self.getOptionalPhaseAttrib("checkNormalised", bool, False))

############################################################
############################################################
############################################################
#Hal generation
############################################################
############################################################
############################################################

class CactusHalGeneratorPhase(CactusPhasesJob):
    def run(self, fileStore):
        referenceNode = findRequiredNode(self.cactusWorkflowArguments.configNode, "reference")
        if referenceNode.attrib.has_key("reference"):
            self.phaseNode.attrib["reference"] = referenceNode.attrib["reference"]
        if self.getOptionalPhaseAttrib("buildFasta", bool, default=False):
            self.phaseNode.attrib["fastaPath"] = self.cactusWorkflowArguments.experimentNode.find("hal").attrib["fastaPath"]
            self.makeRecursiveChildJob(CactusFastaGenerator)
        if self.getOptionalPhaseAttrib("buildHal", bool, default=False):
            self.setupSecondaryDatabase()
            self.phaseNode.attrib["experimentPath"] = self.cactusWorkflowArguments.experimentFile
            self.phaseNode.attrib["secondaryDatabaseString"] = self.cactusWorkflowArguments.secondaryDatabaseString
            #self.phaseNode.attrib["outputFile"]=self.cactusWorkflowArguments.experimentNode.find("hal").attrib["halPath"]
            self.makeFollowOnPhaseJob(CactusHalGeneratorPhase2, "hal")
            self.makeRecursiveChildJob(CactusHalGeneratorRecursion, launchSecondaryKtForRecursiveJob=True)

class CactusFastaGenerator(CactusRecursionJob):
    def run(self, fileStore):
        runCactusFastaGenerator(cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                                    flowerName=decodeFirstFlowerName(self.flowerNames),
                                    outputFile=self.getOptionalPhaseAttrib("fastaPath"),
                                    referenceEventString=self.getOptionalPhaseAttrib("reference"))
            
class CactusHalGeneratorPhase2(CactusHalGeneratorPhase):
    def run(self, fileStore): 
        self.cleanupSecondaryDatabase()

class CactusHalGeneratorRecursion(CactusRecursionJob):
    """Generate the hal file by merging indexed hal files from the children.
    """ 
    def run(self, fileStore):
        logger.debug("Database string: %s" % self.phaseNode.attrib["secondaryDatabaseString"])
        i = extractNode(self.phaseNode)
        if "outputFile" in i.attrib:
            i.attrib.pop("outputFile")
        self.makeRecursiveJobs(phaseNode=i)
        self.makeFollowOnRecursiveJob(CactusHalGeneratorUpWrapper)

class CactusHalGeneratorUpWrapper(CactusRecursionJob):
    """Does the up pass for filling in the coordinates, once a reference is added.
    """ 
    def run(self, fileStore):
        runCactusHalGenerator(cactusDiskDatabaseString=self.cactusDiskDatabaseString, 
                              secondaryDatabaseString=self.getOptionalPhaseAttrib("secondaryDatabaseString"),
                              flowerNames=self.flowerNames,
                              referenceEventString=self.getOptionalPhaseAttrib("reference"), #self.configNode.attrib["reference"], #self.getOptionalPhaseAttrib("reference"), 
                              outputFile=self.getOptionalPhaseAttrib("outputFile"),
                              showOnlySubstitutionsWithRespectToReference=\
                              self.getOptionalPhaseAttrib("showOnlySubstitutionsWithRespectToReference", bool))

class CactusHalGeneratorPhaseCleanup(CactusPhasesJob):
    """Cleanup the database used to build the hal
    """
    def run(self, fileStore):
        self.cleanupSecondaryDatabase()

############################################################
############################################################
############################################################
#Main function
############################################################
############################################################
############################################################
class CactusWorkflowArguments:
    """Object for representing a cactus workflow's arguments
    """
    def __init__(self, options, experimentFile):
        #Get a local copy of the experiment file
        self.experimentFile = experimentFile
        self.experimentNode = ET.parse(self.experimentFile).getroot()
        self.experimentWrapper = ExperimentWrapper(self.experimentNode)
        #Get the database string
        self.cactusDiskDatabaseString = ET.tostring(self.experimentNode.find("cactus_disk").find("st_kv_database_conf")).translate(None, '\n')
        #Get the species tree
        self.speciesTree = self.experimentNode.attrib["species_tree"]
        #Get any list of 'required species' for the blocks of the cactus.
        self.outgroupEventNames = getOptionalAttrib(self.experimentNode, "outgroup_events")
        #Constraints
        self.constraintsFile = getOptionalAttrib(self.experimentNode, "constraints")
        #Secondary, scratch DB
        secondaryConf = copy.deepcopy(self.experimentNode.find("cactus_disk").find("st_kv_database_conf"))
        secondaryElem = DbElemWrapper(secondaryConf)
        dbPath = secondaryElem.getDbDir()
        assert dbPath is not None
        secondaryDbPath = os.path.join(os.path.dirname(dbPath), "%s_tempSecondaryDatabaseDir_%s" % (
            os.path.basename(dbPath), random.random()))
        secondaryElem.setDbDir(secondaryDbPath)
        if secondaryElem.getDbType() == "kyoto_tycoon":
            secondaryElem.setDbPort(secondaryElem.getDbPort() + 100)
        self.secondaryDatabaseString = secondaryElem.getConfString()
            
        #The config node
        self.configNode = ET.parse(self.experimentWrapper.getConfigPath()).getroot()
        self.configWrapper = ConfigWrapper(self.configNode)
        #Now deal with the constants that ned to be added here
        self.configWrapper.substituteAllPredefinedConstantsWithLiterals()
        self.configWrapper.setBuildHal(options.buildHal)
        self.configWrapper.setBuildFasta(options.buildFasta)
        
        #Now build the remaining options from the arguments
        if options.buildAvgs:
            findRequiredNode(self.configNode, "avg").attrib["buildAvgs"] = "1"
        if options.buildReference:
            findRequiredNode(self.configNode, "reference").attrib["buildReference"] = "1"
            

def addCactusWorkflowOptions(parser):
    parser.add_argument("--experiment", dest="experimentFile", 
                      help="The file containing a link to the experiment parameters")
    
    parser.add_argument("--buildAvgs", dest="buildAvgs", action="store_true",
                      help="Build trees", default=False)
    
    parser.add_argument("--buildReference", dest="buildReference", action="store_true",
                      help="Creates a reference ordering for the flowers", default=False)
    
    parser.add_argument("--buildHal", dest="buildHal", action="store_true",
                      help="Build a hal file", default=False)
    
    parser.add_argument("--buildFasta", dest="buildFasta", action="store_true",
                      help="Build a fasta file of the input sequences (and reference sequence, used with hal output)", 
                      default=False)

    parser.add_argument("--test", dest="test", action="store_true",
                      help="Run doctest unit tests")

class RunCactusPreprocessorThenCactusSetup(Job):
    def __init__(self, options):
        Job.__init__(self)
        self.options = options
        
    def run(self, fileStore):
        cactusWorkflowArguments=CactusWorkflowArguments(self.options, fileStore)
        eW = ExperimentWrapper(cactusWorkflowArguments.experimentNode)
        outputSequenceFiles = CactusPreprocessor.getOutputSequenceFiles(eW.getSequences(), eW.getOutputSequenceDir())
        self.addChild(CactusPreprocessor(eW.getSequences(), outputSequenceFiles, cactusWorkflowArguments.configNode))
        #Now make the setup, replacing the input sequences with the preprocessed sequences
        eW.setSequences(outputSequenceFiles)
        fileStore.logToMaster("doTrimStrategy() = %s, outgroupEventNames = %s" % (cactusWorkflowArguments.configWrapper.getDoTrimStrategy(), cactusWorkflowArguments.outgroupEventNames))
        if cactusWorkflowArguments.configWrapper.getDoTrimStrategy() and cactusWorkflowArguments.outgroupEventNames is not None:
            # Use the trimming strategy to blast ingroups vs outgroups.
            self.addFollowOn(CactusTrimmingBlastPhase(cactusWorkflowArguments=cactusWorkflowArguments, phaseName="trimBlast"))
        else:
            self.addFollowOn(CactusSetupPhase(cactusWorkflowArguments=cactusWorkflowArguments,
                                                    phaseName="setup"))
        
def main():
    ##########################################
    #Construct the arguments.
    ##########################################
    
    parser = ArgumentParser()
    Job.Runner.addToilOptions(parser)
    addCactusWorkflowOptions(parser)
        
    options = parser.parse_args()
    if options.test:
        _test()
    setLoggingFromOptions(options)
    
    #if len(args) != 0:
     #   raise RuntimeError("Unrecognised input arguments: %s" % " ".join(args))

    cactusWorkflowArguments = CactusWorkflowArguments(options)
    Job.Runner.startToil(RunCactusPreprocessorThenCactusSetup(options), options)

def _test():
    import doctest      
    return doctest.testmod()

if __name__ == '__main__':
    from cactus.pipeline.cactus_workflow import *
    main()
