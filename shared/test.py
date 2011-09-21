#!/usr/bin/env python

#Copyright (C) 2009-2011 by Benedict Paten (benedictpaten@gmail.com)
#
#Released under the MIT license, see LICENSE.txt
"""Common test functions used for generating inputs to run cactus workflow and running
cactus workflow and the various utilities.
"""

import random
import os
import xml.etree.ElementTree as ET

from sonLib.bioio import logger
from sonLib.bioio import getTempFile
from sonLib.bioio import getTempDirectory
from sonLib.bioio import getRandomSequence
from sonLib.bioio import mutateSequence
from sonLib.bioio import reverseComplement
from sonLib.bioio import fastaWrite
from sonLib.bioio import printBinaryTree
from sonLib.bioio import system
from sonLib.bioio import getRandomAlphaNumericString

from cactus.shared.common import runCactusWorkflow
from cactus.shared.common import runCactusCheck

from sonLib.bioio import TestStatus

from sonLib.tree import makeRandomBinaryTree

from jobTree.test.jobTree.jobTreeTest import runJobTreeStatusAndFailIfNotComplete
from jobTree.src.common import runJobTreeStats

from cactus.shared.config import checkDatabaseConf
from cactus.shared.config import CactusWorkflowExperiment

###########
#Stuff for setting up the experiment configuration file for a test
###########

GLOBAL_DATABASE_CONF = None
BATCH_SYSTEM = None

def getBatchSystem():
    return BATCH_SYSTEM

def getGlobalDatabaseConf():
    return GLOBAL_DATABASE_CONF

def initialiseGlobalDatabaseConf(dataString):
    """Initialises the global database conf string which, if defined,
    is used as the database for CactusWorkflowExperiments."""
    global GLOBAL_DATABASE_CONF
    GLOBAL_DATABASE_CONF = ET.fromstring(dataString)
    checkDatabaseConf(GLOBAL_DATABASE_CONF)
    
def initialiseGlobalBatchSystem(batchSystem):
    """Initialise the global batch system variable.
    """
    global BATCH_SYSTEM
    assert batchSystem in ("singleMachine", "parasol", "gridEngine")
    BATCH_SYSTEM = batchSystem
    
def getCactusWorkflowExperimentForTest(sequences, newickTreeString, outputDir, databaseName=None, configFile=None, singleCopySpecies=None):
    """Wrapper to constructor of CactusWorkflowExperiment which additionally incorporates
    any globally set database conf.
    """
    return CactusWorkflowExperiment(sequences, newickTreeString, outputDir=outputDir, databaseName=databaseName, databaseConf=GLOBAL_DATABASE_CONF, configFile=configFile,
                                    singleCopySpecies=singleCopySpecies)

def parseCactusSuiteTestOptions():
    """Cactus version of the basic option parser that can additionally parse 
    a database conf XML string to be used in experiments."""
    from sonLib.bioio import getBasicOptionParser
    parser = getBasicOptionParser("usage: %prog [options]", "%prog 0.1")
    
    parser.add_option("--databaseConf", dest="databaseConf", type="string",
                      help="Gives a database conf string which will direct all tests to use the given database (see the readme for instructions on setup)",
                      default=None)
    
    parser.add_option("--batchSystem", dest="batchSystem", type="string",
                      help="Set the batch system for the tests to be run under",
                      default=None)
    
    from sonLib.bioio import parseSuiteTestOptions
    options, args = parseSuiteTestOptions(parser)
    #This is the key bit
    if options.databaseConf != None:
        initialiseGlobalDatabaseConf(options.databaseConf)
    
    if options.batchSystem != None:
        initialiseGlobalBatchSystem(options.batchSystem)
        
    return options, args

###############
#Stuff for getting random inputs to a test
###############

def getCactusInputs_random(regionNumber=0, tempDir=None,
                          sequenceNumber=random.choice(xrange(100)), 
                          avgSequenceLength=random.choice(xrange(1, 5000)), 
                          treeLeafNumber=random.choice(xrange(1, 10))):
    """Gets a random set of sequences, each of length given, and a species
    tree relating them. Each sequence is a assigned an event in this tree.
    """
    #Make tree
    binaryTree = makeRandomBinaryTree(treeLeafNumber)
    newickTreeString = printBinaryTree(binaryTree, includeDistances=True)
    newickTreeLeafNames = []
    def fn(tree):
        if tree.internal:
            fn(tree.left)
            fn(tree.right)
        else:
            newickTreeLeafNames.append(tree.iD)
    fn(binaryTree)
    logger.info("Made random binary tree: %s" % newickTreeString)
    
    sequenceDirs = []
    for i in xrange(len(newickTreeLeafNames)):
        seqDir = getTempDirectory(rootDir=tempDir)
        sequenceDirs.append(seqDir)
        
    logger.info("Made a set of random directories: %s" % " ".join(sequenceDirs))
    
    #Random sequences and species labelling
    sequenceFile = None
    fileHandle = None
    parentSequence = getRandomSequence(length=random.choice(xrange(1, 2*avgSequenceLength)))[1]
    for i in xrange(sequenceNumber):
        if sequenceFile == None:
            if random.random() > 0.5: #Randomly choose the files to be attached or not
                suffix = ".fa.complete"
            else:
                suffix = ".fa"
            sequenceFile = getTempFile(rootDir=random.choice(sequenceDirs), suffix=suffix)
            fileHandle = open(sequenceFile, 'w')
        if random.random() > 0.8: #Get a new root sequence
            parentSequence = getRandomSequence(length=random.choice(xrange(1, 2*avgSequenceLength)))[1]
        sequence = mutateSequence(parentSequence, distance=random.random()*0.5)
        name = getRandomAlphaNumericString(15)
        if random.random() > 0.5:
            sequence = reverseComplement(sequence)
        fastaWrite(fileHandle, name, sequence)
        if random.random() > 0.5:
            fileHandle.close()
            fileHandle = None
            sequenceFile = None
    if fileHandle != None:
        fileHandle.close()
        
    logger.info("Made %s sequences in %s directories" % (sequenceNumber, len(sequenceDirs)))
    
    return sequenceDirs, newickTreeString

def parseNewickTreeFile(newickTreeFile):
    fileHandle = open(newickTreeFile, 'r')
    newickTreeString = fileHandle.readline()
    fileHandle.close()
    return newickTreeString

def getInputs(path, sequenceNames):
    """Requires setting SON_TRACE_DATASETS variable and having access to datasets.
    """
    seqPath = os.path.join(TestStatus.getPathToDataSets(), path)
    sequences = [ os.path.join(seqPath, sequence) for sequence in sequenceNames ] #Same order as tree
    newickTreeString = parseNewickTreeFile(os.path.join(path, "tree.newick"))
    return sequences, newickTreeString  

def getCactusInputs_blanchette(regionNumber=0, tempDir=None):
    """Gets the inputs for running cactus_workflow using a blanchette simulated region
    (0 <= regionNumber < 50).
    
    Requires setting SON_TRACE_DATASETS variable and having access to datasets.
    """
    assert regionNumber >= 0
    assert regionNumber < 50
    #regionNumber = 1
    blanchettePath = os.path.join(TestStatus.getPathToDataSets(), "blanchettesSimulation")
    sequences = [ os.path.join(blanchettePath, ("%.2i.job" % regionNumber), species) \
                 for species in ("HUMAN", "CHIMP", "BABOON", "MOUSE", "RAT", "DOG", "CAT", "PIG", "COW") ] #Same order as tree
    newickTreeString = parseNewickTreeFile(os.path.join(blanchettePath, "tree.newick"))
    return sequences, newickTreeString
    
def getCactusInputs_encode(regionNumber=0, tempDir=None):
    """Gets the inputs for running cactus_workflow using an Encode pilot project region.
     (0 <= regionNumber < 15).
    
    Requires setting SON_TRACE_DATASETS variable and having access to datasets.
    """
    assert regionNumber >= 0
    assert regionNumber < 14
    encodeRegionString = "ENm%03i" % (regionNumber+1)
    encodeDatasetPath = os.path.join(TestStatus.getPathToDataSets(), "MAY-2005")
    sequences = [ os.path.join(encodeDatasetPath, encodeRegionString, ("%s.%s.fa" % (species, encodeRegionString))) for\
                species in ("human", "chimp", "baboon", "mouse", "rat", "dog", "cow") ]
    newickTreeString = parseNewickTreeFile(os.path.join(encodeDatasetPath, "reducedTree.newick"))
    return sequences, newickTreeString

def getCactusInputs_chromosomeX(regionNumber=0, tempDir=None):
    """Gets the inputs for running cactus_workflow using an some mammlian chromosome
    X's.
    
    Requires setting SON_TRACE_DATASETS variable and having access to datasets.
    """
    chrXPath = os.path.join(TestStatus.getPathToDataSets(), "chr_x")
    sequences = [ os.path.join(chrXPath, seqFile) for seqFile in ("cow.fa", "dog.fa", "human.fa", "mouse.fa", "rat.fa") ]
    newickTreeString = parseNewickTreeFile(os.path.join(chrXPath, "newickTree.txt"))
    return sequences, newickTreeString

def runWorkflow_TestScript(sequences, newickTreeString, 
                           outputDir=None,
                           batchSystem="single_machine",
                           buildTrees=True, buildFaces=True, buildReference=True,
                           configFile=None,
                           buildJobTreeStats=False,
                           singleCopySpecies=None):
    """Runs the workflow and various downstream utilities.
    """
    logger.info("Running cactus workflow test script")
    logger.info("Got the following sequence dirs/files: %s" % " ".join(sequences))
    logger.info("Got the following tree %s" % newickTreeString)
    
    #Setup the output dir
    assert outputDir != None
    logger.info("Using the output dir: %s" % outputDir)
    
    #Setup the flower disk.
    experiment = getCactusWorkflowExperimentForTest(sequences, newickTreeString, 
                                                    outputDir=outputDir, databaseName=None, 
                                                    configFile=configFile,
                                                    singleCopySpecies=singleCopySpecies)
    experiment.cleanupDatabase()
    cactusDiskDatabaseString = experiment.getDatabaseString()
    experimentFile = os.path.join(outputDir, "experiment.xml")
    experiment.writeExperimentFile(experimentFile)
   
    #Setup the job tree dir.
    jobTreeDir = os.path.join(outputDir, "jobTree")
    logger.info("Got a job tree dir for the test: %s" % jobTreeDir)
    
    #Run the actual workflow
    runCactusWorkflow(experimentFile, jobTreeDir, 
                      batchSystem=batchSystem, buildTrees=buildTrees, 
                      buildFaces=buildFaces, buildReference=buildReference,
                      jobTreeStats=buildJobTreeStats)
    logger.info("Ran the the workflow")
    
    #Check if the jobtree completed sucessively.
    runJobTreeStatusAndFailIfNotComplete(jobTreeDir)
    logger.info("Checked the job tree dir")
    
    #Check if the cactusDisk is okay..
    runCactusCheck(cactusDiskDatabaseString, recursive=True) #This should also occur during the workflow, so this
    #is redundant, but defensive
    logger.info("Checked the cactus tree")
    
    #Now run various utilities..
    if buildJobTreeStats:
        jobTreeStatsFile = os.path.join(outputDir, "jobTreeStats.xml")
        runJobTreeStats(jobTreeDir, jobTreeStatsFile)
        
    #Now remove everything we generate
    system("rm -rf %s %s" % (jobTreeDir, experimentFile))   
    
    #Cleanup the experiment
    return experiment
        
testRestrictions_NotShort = ()
        
def runWorkflow_multipleExamples(inputGenFunction,
                                 testNumber=1, 
                                 testRestrictions=(TestStatus.TEST_SHORT, TestStatus.TEST_MEDIUM, \
                                                   TestStatus.TEST_LONG, TestStatus.TEST_VERY_LONG,),
                               inverseTestRestrictions=False,
                               batchSystem="single_machine",
                               buildTrees=True, buildFaces=True, buildReference=True,
                               configFile=None, buildJobTreeStats=False, singleCopySpecies=None):
    """A wrapper to run a number of examples.
    """
    if (inverseTestRestrictions and TestStatus.getTestStatus() not in testRestrictions) or \
        (not inverseTestRestrictions and TestStatus.getTestStatus() in testRestrictions):
        for test in xrange(testNumber): 
            tempDir = getTempDirectory(os.getcwd())
            sequences, newickTreeString = inputGenFunction(regionNumber=test, tempDir=tempDir)
            experiment = runWorkflow_TestScript(sequences, newickTreeString,
                                                outputDir=tempDir,
                                   batchSystem=batchSystem,
                                   buildTrees=buildTrees, buildFaces=buildFaces, buildReference=buildReference, 
                                   configFile=configFile,
                                   buildJobTreeStats=buildJobTreeStats,
                                   singleCopySpecies=singleCopySpecies)
            experiment.cleanupDatabase()
            system("rm -rf %s" % tempDir)
            logger.info("Finished random test %i" % test)
    
