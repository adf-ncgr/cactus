#!/usr/bin/env python

#Copyright (C) 2011 by Glenn Hickey
#
#Released under the MIT license, see LICENSE.txt

""" Compute outgroups for subtrees by greedily finding
the nearest valid candidate.  has option to only assign
leaves as outgroups

"""

import os
import xml.etree.ElementTree as ET
import sys
import math
import copy
import collections
import itertools
import networkx as NX
from collections import defaultdict, namedtuple
from optparse import OptionParser

from cactus.progressive.multiCactusProject import MultiCactusProject
from cactus.progressive.multiCactusTree import MultiCactusTree
from sonLib.bioio import fastaRead

class GreedyOutgroup(object):
    def __init__(self):
        self.dag = None
        self.dm = None
        self.dmDirected = None
        self.root = None
        self.ogMap = None
        self.mcTree = None
        
    # add edges from sonlib tree to self.dag
    # compute self.dm: an undirected distance matrix
    def importTree(self, mcTree):
        self.mcTree = mcTree
        self.dag = mcTree.nxDg.copy()
        self.root = mcTree.rootId
        self.stripNonEvents(self.root, mcTree.subtreeRoots)
        self.dmDirected = NX.algorithms.shortest_paths.weighted.\
        all_pairs_dijkstra_path_length(self.dag)
        graph = NX.Graph(self.dag)
        self.dm = NX.algorithms.shortest_paths.weighted.\
        all_pairs_dijkstra_path_length(graph)
        self.ogMap = defaultdict(list)
 
    # get rid of any node that's not an event
    def stripNonEvents(self, id, subtreeRoots):
        children = []
        for outEdge in self.dag.out_edges(id):
            children.append(outEdge[1])
        parentCount = len(self.dag.in_edges(id))
        assert parentCount <= 1
        parent = None
        if parentCount == 1:
            parent = self.dag.in_edges[0][0]
        if id not in subtreeRoots and len(children) >= 1:
            assert parentCount == 1
            self.dag.remove_node(id)
            for child in children:
                self.dag.add_edge(parent, child)
                self.stripNonEvents(child, subtreeRoots)
    
    # are source and sink son same path to to root? if so,
    # they shouldn't be outgroups of each other.             
    def onSamePath(self, source, sink):
        if source in self.dmDirected:
            if sink in self.dmDirected[source]:
                return True
        if sink in self.dmDirected:
            if source in self.dmDirected[sink]:
                return True 
        return False
    
    # fill up a dictionary of node id -> height in tree where
    # leaves have height = 0
    def heightTable(self, node, htable):
        children = [x[1] for x in self.dag.out_edges(node)]
        if len(children) == 0:
            htable[node] = 0
        else:            
            for i in children:
                self.heightTable(i, htable)
            htable[node] = max([htable[i] for i in children]) + 1

    # check the candidate using the set and and fraction
    def inCandidateSet(self, node, candidateChildFrac):
        if self.candidateMap is None or len(self.candidateMap) == 0:
            return True
        if self.mcTree.getName(node) in self.candidateMap:
            return self.candidateMap[self.mcTree.getName(node)]
        children = self.mcTree.breadthFirstTraversal(node)
        leaves = []
        for child in children:
            if self.mcTree.isLeaf(child):
                leaves.append(child)
        candidateLeaves = 0
        for leaf in leaves:
            if self.mcTree.getName(leaf) in self.candidateMap:
                candidateLeaves += 1
        if len(leaves) == 0:
            self.candidateMap[self.mcTree.getName(node)] = False
            return False
        frac = float(candidateLeaves) / float(len(leaves))
        if frac >= candidateChildFrac:
            self.candidateMap[self.mcTree.getName(node)] = True
            return True
        self.candidateMap[self.mcTree.getName(node)] = False
        return False

    def clearOutgroupAssignments(self):
        self.ogMap = defaultdict(list)
        self.dag = mcTree.nxDg.copy()

    # greedily assign closest possible valid outgroups
    # If some outgroups are already assigned, keep the existing
    # assignments but attempt to add more, if possible.
    # all outgroups are stored in self.ogMap
    # edges between leaves ARE NOT kept in the dag    
    # the threshold parameter specifies how much parallelism can
    # be sacrificed by the selection of an outgroup
    # threshold = None : just greedy with no constraints
    # threshold = 0 : depth of schedule guaranteed to be unaffected by outgroup
    # threshold = k : depth increases by at most k per outgroup
    # candidateSet : names of valid outgroup genomes. (all if None)
    # candidateChildFrac : min fraction of children of ancestor in
    # candidateSet in order for the ancestor to be an outgroup candidate
    # if > 1, then only members of the candidate set and none of their
    # ancestors are chosen
    # maxNumOutgroups : max number of outgroups to put in each entry of self.ogMap
    def greedy(self, threshold = None, candidateSet = None,
               candidateChildFrac = 2., maxNumOutgroups = 1):
        orderedPairs = []
        for source, sinks in self.dm.items():
            for sink, dist in sinks.items():
                if source != self.root and sink != self.root:
                    orderedPairs.append((dist, (source, sink)))
        orderedPairs.sort(key = lambda x: x[0])
        finished = set()
        self.candidateMap = dict()
        if candidateSet is not None:
            assert isinstance(candidateSet, set)
            for candidate in candidateSet:
                self.candidateMap[candidate] = True

        htable = dict()
        self.heightTable(self.root, htable)

        for candidate in orderedPairs:
            source = candidate[1][0]
            sink = candidate[1][1]
            sourceName = self.mcTree.getName(source)
            sinkName = self.mcTree.getName(sink)
            dist = candidate[0]

            # skip leaves (as sources)
            if len(self.dag.out_edges(source)) == 0:
                finished.add(source)

            # skip nodes that were already finished in a previous run
            if sourceName in self.ogMap and len(self.ogMap[sourceName]) >= maxNumOutgroups:
                finished.add(source)

            # skip nodes that aren't in the candidate set (if specified)
            # or don't have enough candidate children
            if not self.inCandidateSet(sink, candidateChildFrac):
                continue

            # canditate pair exceeds given threshold, so we skip
            if threshold is not None and \
            htable[sink] - htable[source] + 1 > threshold:
                continue

            if source not in finished and \
            not self.onSamePath(source, sink):
                self.dag.add_edge(source, sink, weight=dist, info='outgroup')
                if NX.is_directed_acyclic_graph(self.dag):
                    htable[source] = max(htable[source], htable[sink] + 1)
                    existingOutgroups = [i[0] for i in self.ogMap[sourceName]]
                    if sinkName in existingOutgroups:
                        # This outgroup was already assigned to this source in a previous run
                        # Sanity check that the distance is equal
                        existingOutgroupDist = dict(self.ogMap[sourceName])
                        assert existingOutgroupDist[sinkName] == dist
                        continue
                    self.ogMap[sourceName].append((sinkName, dist))
                    if len(self.ogMap[sourceName]) >= maxNumOutgroups:
                        finished.add(source)
                else:
                    self.dag.remove_edge(source, sink)

        # Since we could be adding to the ogMap instead of creating
        # it, sort the outgroups by distance again. Sorting the
        # outgroups is critical for the multiple-outgroups code to
        # work well.
        for node, outgroups in self.ogMap.items():
            self.ogMap[node] = sorted(outgroups, key=lambda x: x[1])


# First stab at better outgroup selection.  Uses estimated fraction of
# orthologous bases between two genomes, along with dynamic programming
# to select outgroups to best create ancestors.
#
# Only works with leaves for now (ie will never choose ancestor as outgroup)
#
class DynamicOutgroup(GreedyOutgroup):
    def __init__(self, numOG):
        self.SeqInfo = namedtuple("SeqInfo", "count totalLen")
        self.sequenceInfo = None
        self.numOG = numOG
        assert self.numOG is not None
        self.defaultBranchLength = 0.1

    # create map of leaf id -> sequence stats by scanninf the FASTA
    # files.  will be used to determine assembly quality for each input
    # genome (in a very crude manner, at least to start).
    # 
    # for internal nodes, we store the stats of the max leaf underneath
    def importTree(self, mcTree, seqMap):
        super(DynamicOutgroup, self).importTree(mcTree)
        
        assert seqMap is not None
        # map name to (numSequences, totalLength)
        self.sequenceInfo = dict()
        for event, inPath in seqMap.items():
            node = self.mcTree.getNodeId(event)
            if os.path.isdir(inPath):
                fastaPaths = [os.path.join(inPath, f) for
                              f in os.listdir(inPath)]
            else:
                fastaPaths = [inPath]
            for faPath in fastaPaths:
                if not os.path.isfile(faPath):
                    raise RuntimeError("Unable to open sequence file %s" %
                                       faPath)
                faFile = open(faPath, "r")
                count, totalLen = 0, 0
                for name, seq in fastaRead(faFile):
                    count += 1
                    totalLen += len(seq)
                faFile.close()
            self.sequenceInfo[node] = self.SeqInfo(count, totalLen)

            # propagate leaf stats up to the root
            # can speed this up by O(N) but not sure if necessary..
            # we are conservative here in that we assume that the
            # ancestor has the longest, least fragmented genome possible
            # when judging from its descendants. 
            x = node
            while self.mcTree.hasParent(x):
                x = self.mcTree.getParent(x)
                if x not in self.sequenceInfo:
                    self.sequenceInfo[x] = self.SeqInfo(count, totalLen)
                else:
                    self.sequenceInfo[x] = self.SeqInfo(
                        min(count, self.sequenceInfo[x].count),
                        max(totalLen, self.sequenceInfo[x].totalLen))

    # run the dynamic programming algorithm on each internal node
    def compute(self):
        self.ogMap = dict()
        for node in self.mcTree.breadthFirstTraversal():
            if self.mcTree.isLeaf(node):
                continue
            self.__dpInit(node)
            self.__dpRun(node)
            nodeName = self.mcTree.getName(node)
            self.ogMap[nodeName] = self.dpTable[node][self.numOG].solution
            for og in self.ogMap[nodeName]:
                self.dag.add_edge(node, og)
                print self.dpTree.getName(node), "->", self.dpTree.getName(og)

                
    # initialize dynamic programming table
    def __dpInit(self, ancestralNodeId):
        self.dpTree = copy.deepcopy(self.mcTree)
        self.rootSeqInfo = self.sequenceInfo[self.dpTree.getRootId()]
        self.branchProbs = dict()
        self.DPEntry = namedtuple("DPEntry", "score solution")
        # map .node id to [(score, solution)]
        # where list is for 0, 1, 2, ... k (ie best score for solution
        # of size k)
        self.dpTable = dict()

        # make a new tree rooted at the target ancestor with everything
        # below it, ie invalid outgroups, cut out
        for child in self.dpTree.getChildren(ancestralNodeId):
            self.dpTree.removeEdge(ancestralNodeId, child)
        self.dpTree.reroot(ancestralNodeId)

        # compute all the branch conservation probabilities
        for node in self.dpTree.preOrderTraversal():
            if self.dpTree.hasParent(node):
                self.branchProbs[node] = self.__computeBranchConservation(node)

        # set table to 0
        for node in self.dpTree.preOrderTraversal():
            self.dpTable[node] = []
            for i in xrange(self.numOG + 1):
                self.dpTable[node].append(self.DPEntry(0.0, []))
                
    # compute score for given node from its children using the dynamic
    # programming table
    def __dpNode(self, node):
        children = self.dpTree.getChildren(node)
        numChildren = len(children)
        # special case for leaf
        if numChildren == 0:
            self.dpTable[node][1] = self.DPEntry(1.0, [node])
        else:
            # iterate all possible combinations of child solution sizes
            # (very inefficeint since we only want unique solutions with
            # sum <= numOG, but assume numbers are small enough so doesn't
            # matter for now)
            cset = [x for x in xrange(0, self.numOG + 1)]
            for scoreAlloc in itertools.product(*[cset] * numChildren):
                csetK = sum(scoreAlloc)
                if  csetK > self.numOG:
                    continue
                # we compute the probability that a base is lost along
                # all the branches (so will be a product of of complement
                # of conservations along each branch)
                lossProb = 0.
                solution = []
                for childNo, childId in enumerate(children):
                    childK = scoreAlloc[childNo]
                    childCons = self.dpTable[childId][childK].score
                    lossProb *= (1. - self.branchProbs[childId] * childCons)
                    solution += self.dpTable[childId][childK].solution
                # overall conservation is 1 - loss
                consProb = 1. - lossProb
                assert consProb >= 0. and consProb <= 1.
                assert len(solution) <= csetK
                if consProb > self.dpTable[node][csetK].score and \
                  len(solution) == csetK:
                    self.dpTable[node][csetK] = self.DPEntry(consProb, solution)
                    
    # get the dynamic programming solution (for a single ancestor set in
    # __dpInit...)
    def __dpRun(self, node):
        for child in self.dpTree.getChildren(node):
            self.__dpRun(child)
        self.__dpNode(node)
        
    # compute the probability that a base is not "lost" on a branch
    # from given node to its parent
    def __computeBranchConservation(self, node):
        nodeInfo = self.sequenceInfo[node]
        
        # Loss probablity computed from genome length ratio
        lenFrac = float(nodeInfo.totalLen) / float(self.rootSeqInfo.totalLen)
        pLoss = max(0., 1. - lenFrac)

        # Fragmentation probability
        numExtraFrag = max(0, nodeInfo.count - self.rootSeqInfo.count)
        pFrag = float(numExtraFrag) / float(self.rootSeqInfo.totalLen)

        # Mutation probability
        branchLength = self.dpTree.getWeight(self.dpTree.getParent(node),
                                             node, None)
        if branchLength is None or branchLength < 0 or branchLength >= 1:
            # some kind of warning should happen here
            branchLength = self.defaultBranchLength
        jcMutProb = .75 - .75 * math.exp(-branchLength)

        conservationProb = (1. - pLoss) * (1. - pFrag) * (1. - jcMutProb)
        return conservationProb        

            
def main():
    usage = "usage: %prog <project> <output graphviz .dot file>"
    description = "TEST: draw the outgroup DAG"
    parser = OptionParser(usage=usage, description=description)
    parser.add_option("--justLeaves", dest="justLeaves", action="store_true", 
                      default = False, help="Assign only leaves as outgroups")
    parser.add_option("--threshold", dest="threshold", type='int',
                      default = None, help="greedy threshold")
    parser.add_option("--numOutgroups", dest="maxNumOutgroups",
                      help="Maximum number of outgroups to provide", type=int)
    parser.add_option("--dynamic", help="Use new dynamic programming"
                      " algorithm", action="store_true", default=False)
    options, args = parser.parse_args()
    
    if len(args) != 2:
        parser.print_help()
        raise RuntimeError("Wrong number of arguments")

    proj = MultiCactusProject()
    proj.readXML(args[0])
    if not options.dynamic:
        outgroup = GreedyOutgroup()
        outgroup.importTree(proj.mcTree)
        if options.justLeaves:
            candidates = set([proj.mcTree.getName(x)
                            for x in proj.mcTree.getLeaves()])
        else:
            candidates = None
        outgroup.greedy(threshold=options.threshold, candidateSet=candidates,
                        candidateChildFrac=1.1)
    else:
        outgroup = DynamicOutgroup(options.maxNumOutgroups)
        seqMap = dict()
        for leaf in proj.mcTree.getLeaves():
            name = proj.mcTree.getName(leaf)
            seqMap[name] = proj.sequencePath(name)
        outgroup.importTree(proj.mcTree, seqMap)
        outgroup.compute()
        
    NX.drawing.nx_agraph.write_dot(outgroup.dag, args[1])
    return 0

if __name__ == '__main__':    
    main()
