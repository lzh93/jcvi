"""
parses tabular BLAST -m8 (-format 6 in BLAST+) format
"""

import os
import os.path as op
import sys
import math
import logging

from itertools import groupby
from collections import defaultdict
from optparse import OptionParser

import numpy as np

from jcvi.formats.base import LineFile, must_open
from jcvi.formats.coords import print_stats
from jcvi.formats.sizes import Sizes
from jcvi.utils.grouper import Grouper
from jcvi.utils.range import range_distance
from jcvi.apps.base import ActionDispatcher, debug, set_outfile, sh, popen
debug()


class BlastLine(object):
    __slots__ = ('query', 'subject', 'pctid', 'hitlen', 'nmismatch', 'ngaps', \
                 'qstart', 'qstop', 'sstart', 'sstop', 'evalue', 'score', \
                 'qseqid', 'sseqid', 'qi', 'si', 'orientation')

    def __init__(self, sline):
        args = sline.split("\t")
        self.query = args[0]
        self.subject = args[1]
        self.pctid = float(args[2])
        self.hitlen = int(args[3])
        self.nmismatch = int(args[4])
        self.ngaps = int(args[5])
        self.qstart = int(args[6])
        self.qstop = int(args[7])
        self.sstart = int(args[8])
        self.sstop = int(args[9])
        self.evalue = float(args[10])
        self.score = float(args[11])

        if self.sstart > self.sstop:
            self.sstart, self.sstop = self.sstop, self.sstart
            self.orientation = '-'
        else:
            self.orientation = '+'

    def __repr__(self):
        return "BlastLine('%s' to '%s', eval=%.3f, score=%.1f)" % \
                (self.query, self.subject, self.evalue, self.score)

    def __str__(self):
        args = [getattr(self, attr) for attr in BlastLine.__slots__[:12]]
        if self.orientation == '-':
            args[8], args[9] = args[9], args[8]
        return "\t".join(str(x) for x in args)

    @property
    def swapped(self):
        """
        Swap query and subject.
        """
        args = [getattr(self, attr) for attr in BlastLine.__slots__[:12]]
        args[0:2] = [self.subject, self.query]
        args[6:10] = [self.sstart, self.sstop, self.qstart, self.qstop]
        if self.orientation == '-':
            args[8], args[9] = args[9], args[8]
        return "\t".join(str(x) for x in args)

    @property
    def bedline(self):
        return "\t".join(str(x) for x in \
                (self.subject, self.sstart - 1, self.sstop, self.query,
                 self.score, self.orientation))


class BlastSlow (LineFile):
    """
    Load entire blastfile into memory
    """
    def __init__(self, filename, sorted=False):
        super(BlastSlow, self).__init__(filename)
        fp = must_open(filename)
        for row in fp:
            self.append(BlastLine(row))
        if not sorted:
            self.sort(key=lambda x: x.query)

    def iter_hits(self):
        for query, blines in groupby(self, key=lambda x: x.query):
            yield query, blines


class Blast (LineFile):
    """
    We can have a Blast class that loads entire file into memory, this is
    not very efficient for big files (BlastSlow); when the BLAST file is
    generated by BLAST/BLAT, the file is already sorted
    """
    def __init__(self, filename):
        super(Blast, self).__init__(filename)
        self.fp = must_open(filename)

    def iter_line(self):
        for row in self.fp:
            yield BlastLine(row)

    def iter_hits(self):
        for query, blines in groupby(self.fp,
                key=lambda x: BlastLine(x).query):
            blines = [BlastLine(x) for x in blines]
            blines.sort(key=lambda x: -x.score)  # descending score
            yield query, blines

    def iter_best_hit(self, N=1, hsps=False):
        for query, blines in groupby(self.fp,
                key=lambda x: BlastLine(x).query):
            blines = [BlastLine(x) for x in blines]
            blines.sort(key=lambda x: -x.score)
            xlines = blines[:N]
            if hsps:
                selected = set(x.subject for x in xlines)
                xlines = [x for x in blines if x.subject in selected]

            for x in xlines:
                yield query, x

    @property
    def hits(self):
        """
        returns a dict with query => blastline
        """
        return dict(self.iter_hits())

    @property
    def best_hits(self):
        """
        returns a dict with query => best blasthit
        """
        return dict(self.iter_best_hit())


def get_stats(blastfile):

    from jcvi.utils.range import range_union

    logging.debug("report stats on `%s`" % blastfile)
    fp = open(blastfile)
    ref_ivs = []
    qry_ivs = []
    identicals = 0
    alignlen = 0

    for row in fp:
        c = BlastLine(row)
        qstart, qstop = c.qstart, c.qstop
        if qstart > qstop:
            qstart, qstop = qstop, qstart
        qry_ivs.append((c.query, qstart, qstop))

        sstart, sstop = c.sstart, c.sstop
        if sstart > sstop:
            sstart, sstop = sstop, sstart
        ref_ivs.append((c.subject, sstart, sstop))

        alen = sstop - sstart
        alignlen += alen
        identicals += c.pctid / 100. * alen

    qrycovered = range_union(qry_ivs)
    refcovered = range_union(ref_ivs)
    id_pct = identicals * 100. / alignlen

    return qrycovered, refcovered, id_pct


def filter(args):
    """
    %prog filter test.blast

    Produce a new blast file and filter based on score.
    """
    p = OptionParser(filter.__doc__)
    p.add_option("--score", dest="score", default=0, type="int",
            help="Score cutoff [default: %default]")
    p.add_option("--pctid", dest="pctid", default=95, type="int",
            help="Percent identity cutoff [default: %default]")
    p.add_option("--hitlen", dest="hitlen", default=100, type="int",
            help="Hit length cutoff [default: %default]")
    p.add_option("--evalue", default=.01, type="float",
            help="E-value cutoff [default: %default]")

    opts, args = p.parse_args(args)
    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    fp = must_open(blastfile)

    score, pctid, hitlen, evalue = \
            opts.score, opts.pctid, opts.hitlen, opts.evalue
    newblastfile = blastfile + ".P{0}L{1}".format(pctid, hitlen)
    fw = must_open(newblastfile, "w")
    for row in fp:
        if row[0] == '#':
            continue
        c = BlastLine(row)

        if c.score < score:
            continue
        if c.pctid < pctid:
            continue
        if c.hitlen < hitlen:
            continue
        if c.evalue > evalue:
            continue

        print >> fw, row.rstrip()

    return newblastfile


def main():

    actions = (
        ('summary', 'provide summary on id% and cov%'),
        ('completeness', 'print completeness statistics for each query'),
        ('annotation', 'create tabular file with the annotations'),
        ('top10', 'count the most frequent 10 hits'),
        ('filter', 'filter BLAST file (based on score, id%, alignlen)'),
        ('covfilter', 'filter BLAST file (based on id% and cov%)'),
        ('cscore', 'calculate C-score for BLAST pairs'),
        ('best', 'get best BLAST hit per query'),
        ('pairs', 'print paired-end reads of BLAST tabular file'),
        ('bed', 'get bed file from BLAST tabular file'),
        ('chain', 'chain adjacent HSPs together'),
        ('swap', 'swap query and subjects in BLAST tabular file'),
        ('sort', 'sort lines so that query grouped together and scores desc'),
        ('mismatches', 'print out histogram of mismatches of HSPs'),
        ('annotate', 'annotate overlap types in BLAST tabular file'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def annotation(args):
    """
    %prog annotation blastfile > annotations

    Create simple two column files from the first two coluns in blastfile. Use
    --queryids and --subjectids to switch IDs or descriptions.
    """
    from jcvi.formats.base import DictFile

    p = OptionParser(annotation.__doc__)
    p.add_option("--queryids", help="Query IDS file to switch [default: %default]")
    p.add_option("--subjectids", help="Subject IDS file to switch [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    d = "\t"
    qids = DictFile(opts.queryids, delimiter=d) if opts.queryids else None
    sids = DictFile(opts.subjectids, delimiter=d) if opts.subjectids else None
    blast = Blast(blastfile)
    for b in blast.iter_line():
        query, subject = b.query, b.subject
        if qids:
            query = qids[query]
        if sids:
            subject = sids[subject]
        print "\t".join((query, subject))


def completeness(args):
    """
    %prog completeness blastfile query.fasta > outfile

    Print statistics for each gene, the coverage of the alignment onto the best hit
    in AllGroup.niaa, as an indicator for completeness of the gene model.
    """
    from jcvi.utils.range import range_minmax

    p = OptionParser(completeness.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    blastfile, fastafile = args
    f = Sizes(fastafile).mapping

    b = BlastSlow(blastfile)
    for query, blines in groupby(b, key=lambda x: x.query):
        blines = list(blines)
        ranges = [(x.sstart, x.sstop) for x in blines]
        b = blines[0]
        query, subject = b.query, b.subject

        rmin, rmax = range_minmax(ranges)
        subject_len = f[subject]

        nterminal_dist = rmin - 1
        cterminal_dist = subject_len - rmax + 1
        print "\t".join(str(x) for x in (b.query, b.subject,
            nterminal_dist, cterminal_dist))


def annotate(args):
    """
    %prog annotate blastfile query.fasta subject.fasta

    Annotate overlap types (dovetail, contained, etc) in BLAST tabular file.
    """
    from jcvi.assembly.goldenpath import Overlap, Overlap_types

    p = OptionParser(annotate.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    blastfile, afasta, bfasta = args
    fp = open(blastfile)
    asizes = Sizes(afasta).mapping
    bsizes = Sizes(bfasta).mapping
    for row in fp:
        b = BlastLine(row)
        asize = asizes[b.query]
        bsize = bsizes[b.subject]
        ov = Overlap(b, asize, bsize)
        print "{0}\t{1}".format(b, Overlap_types[ov.get_otype()])


def top10(args):
    """
    %prog top10 blastfile.best

    Count the most frequent 10 hits. Usually the BLASTFILE needs to be screened
    the get the best match. You can also provide an .ids file to query the ids.
    For example the ids file can contain the seqid to species mapping.

    The ids file is two-column, and can sometimes be generated by
    `jcvi.formats.fasta ids --description`.
    """
    from jcvi.formats.base import DictFile

    p = OptionParser(top10.__doc__)
    p.add_option("--ids", default=None,
                help="Two column ids file to query seqid [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    mapping = DictFile(opts.ids, delimiter="\t") if opts.ids else {}

    cmd = "cut -f2 {0}".format(blastfile)
    cmd += " | sort | uniq -c | sort -k1,1nr | head"
    fp = popen(cmd)
    for row in fp:
        count, seqid = row.split()
        nseqid = mapping.get(seqid, seqid)
        print "\t".join((count, nseqid))


def sort(args):
    """
    %prog sort <blastfile|coordsfile>

    Sort lines so that same query grouped together with scores descending. The
    sort is 'in-place'.
    """
    p = OptionParser(sort.__doc__)
    p.add_option("--query", default=False, action="store_true",
            help="Sort by query position [default: %default]")
    p.add_option("--ref", default=False, action="store_true",
            help="Sort by reference position [default: %default]")
    p.add_option("--coords", default=False, action="store_true",
            help="File is .coords generated by NUCMER [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    if opts.coords:
        if opts.query:
            key = "-k13,13 -k3,3n"
        elif opts.ref:
            key = "-k12,12 -k1,1n"

    else:
        if opts.query:
            key = "-k1,1 -k7,7n"
        elif opts.ref:
            key = "-k2,2 -k9,9n"
        else:
            key = "-k1,1 -k12,12nr"

    cmd = "sort {0} {1} -o {1}".format(key, blastfile)
    sh(cmd)


def cscore(args):
    """
    %prog cscore blastfile > cscoreOut

    See supplementary info for sea anemone genome paper, C-score formula:

        cscore(A,B) = score(A,B) /
             max(best score for A, best score for B)

    A C-score of one is the same as reciprocal best hit (RBH).

    Output file will be 3-column (query, subject, cscore). Use --cutoff to
    select a different cutoff.
    """
    p = OptionParser(cscore.__doc__)
    p.add_option("--cutoff", default=.9999, type="float",
            help="Minimum C-score to report [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    blast = Blast(blastfile)
    logging.debug("Register best scores ..")
    best_score = defaultdict(float)
    for b in blast.iter_line():
        if b.score > best_score[b.query]:
            best_score[b.query] = b.score
        if b.score > best_score[b.subject]:
            best_score[b.subject] = b.score

    blast = Blast(blastfile)
    pairs = defaultdict(float)
    for b in blast.iter_line():
        s = b.score / max(best_score[b.query], best_score[b.subject])
        if s > opts.cutoff:
            pair = (b.query, b.subject)
            if s > pairs[pair]:
                pairs[pair] = s

    for (query, subject), s in sorted(pairs.items()):
        print "\t".join((query, subject, "{0:.2f}".format(s)))


def get_distance(a, b, xaxis=True):
    """
    Returns the distance between two blast HSPs.
    """
    if xaxis:
        arange = ("0", a.qstart, a.qstop, a.orientation)  # 0 is the dummy chromosome
        brange = ("0", b.qstart, b.qstop, b.orientation)
    else:
        arange = ("0", a.sstart, a.sstop, a.orientation)
        brange = ("0", b.sstart, b.sstop, b.orientation)

    dist, oo = range_distance(arange, brange, distmode="ee")
    dist = abs(dist)

    return dist


def combine_HSPs(a):
    """
    Combine HSPs into a single BlastLine.
    """
    m = a[0]
    if len(a) == 1:
        return m

    for b in a[1:]:
        assert m.query == b.query
        assert m.subject == b.subject
        assert m.orientation == b.orientation
        m.hitlen += b.hitlen
        m.nmismatch += b.nmismatch
        m.ngaps += b.ngaps
        m.qstart = min(m.qstart, b.qstart)
        m.qstop = max(m.qstop, b.qstop)
        m.sstart = min(m.sstart, b.sstart)
        m.sstop = max(m.sstop, b.sstop)
        m.score += b.score

    m.pctid = 100 - (m.nmismatch + m.ngaps) * 100. / m.hitlen
    return m


def chain_HSPs(blastlines, xdist=100, ydist=100):
    """
    Take a list of BlastLines (or a BlastSlow instance), and returns a list of
    BlastLines.
    """
    key = lambda x: (x.query, x.subject)
    blastlines.sort(key=key)

    clusters = Grouper()
    for qs, points in groupby(blastlines, key=key):
        points = sorted(list(points), \
                key=lambda x: (x.qstart, x.qstop, x.sstart, x.sstop))

        n = len(points)
        for i in xrange(n):
            a = points[i]
            clusters.join(a)
            for j in xrange(i + 1, n):
                b = points[j]
                if a.orientation != b.orientation:
                    continue

                # x-axis distance
                del_x = get_distance(a, b)
                if del_x > xdist:
                    continue
                # y-axis distance
                del_y = get_distance(a, b, xaxis=False)
                if del_y > ydist:
                    continue
                # otherwise join
                clusters.join(a, b)

    chained_hsps = []
    for c in clusters:
        chained_hsps.append(combine_HSPs(c))
    chained_hsps = sorted(chained_hsps, key=lambda x: -x.score)

    return chained_hsps


def chain(args):
    """
    %prog chain blastfile

    Chain adjacent HSPs together to form larger HSP. The adjacent HSPs have to
    share the same orientation.
    """
    p = OptionParser(chain.__doc__)
    p.add_option("--dist", dest="dist",
            default=100, type="int",
            help="extent of flanking regions to search [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    dist = opts.dist
    assert dist > 0

    blast = BlastSlow(blastfile)
    chained_hsps = chain_HSPs(blast, xdist=dist, ydist=dist)
    for b in chained_hsps:
        print b


def mismatches(args):
    """
    %prog mismatches blastfile

    Print out histogram of mismatches of HSPs, usually for evaluating SNP level.
    """
    from jcvi.utils.cbook import percentage
    from jcvi.graphics.histogram import stem_leaf_plot

    p = OptionParser(mismatches.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    data = []
    b = Blast(blastfile)
    for query, bline in b.iter_best_hit():
        mm = bline.nmismatch + bline.ngaps
        data.append(mm)

    nonzeros = [x for x in data if x != 0]
    title = "Polymorphic sites: {0}".\
            format(percentage(len(nonzeros), len(data)))
    stem_leaf_plot(data, 0, 20, 20, title=title)


def covfilter(args):
    """
    %prog covfilter blastfile fastafile

    Fastafile is used to get the sizes of the queries. Two filters can be
    applied, the id% and cov%.
    """
    p = OptionParser(covfilter.__doc__)
    p.add_option("--pctid", dest="pctid", default=90, type="int",
            help="Percentage identity cutoff [default: %default]")
    p.add_option("--pctcov", dest="pctcov", default=50, type="int",
            help="Percentage identity cutoff [default: %default]")
    p.add_option("--ids", dest="ids", default=None,
            help="Print out the ids that satisfy [default: %default]")
    p.add_option("--list", dest="list", default=False, action="store_true",
            help="List the id% and cov% per gene [default: %default]")
    set_outfile(p, outfile=None)

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    from jcvi.algorithms.supermap import supermap

    blastfile, fastafile = args
    sizes = Sizes(fastafile).mapping
    querysupermap = blastfile + ".query.supermap"
    if not op.exists(querysupermap):
        supermap(blastfile, filter="query")

    blastfile = querysupermap
    assert op.exists(blastfile)

    covered = 0
    mismatches = 0
    gaps = 0
    alignlen = 0
    queries = set()
    valid = set()
    blast = BlastSlow(querysupermap)
    for query, blines in blast.iter_hits():
        blines = list(blines)
        queries.add(query)

        # per gene report
        this_covered = 0
        this_alignlen = 0
        this_mismatches = 0
        this_gaps = 0

        for b in blines:
            this_covered += abs(b.qstart - b.qstop + 1)
            this_alignlen += b.hitlen
            this_mismatches += b.nmismatch
            this_gaps += b.ngaps

        this_identity = 100. - (this_mismatches + this_gaps) * 100. / this_alignlen
        this_coverage = this_covered * 100. / sizes[query]

        if opts.list:
            print "{0}\t{1:.1f}\t{2:.1f}".format(query, this_identity, this_coverage)

        if this_identity >= opts.pctid and this_coverage >= opts.pctcov:
            valid.add(query)

        covered += this_covered
        mismatches += this_mismatches
        gaps += this_gaps
        alignlen += this_alignlen

    mapped_count = len(queries)
    valid_count = len(valid)
    cutoff_message = "(id={0.pctid}% cov={0.pctcov}%)".format(opts)

    print >> sys.stderr, "Identity: {0} mismatches, {1} gaps, {2} alignlen".\
            format(mismatches, gaps, alignlen)
    total = len(sizes.keys())
    print >> sys.stderr, "Total mapped: {0} ({1:.1f}% of {2})".\
            format(mapped_count, mapped_count * 100. / total, total)
    print >> sys.stderr, "Total valid {0}: {1} ({2:.1f}% of {3})".\
            format(cutoff_message, valid_count, valid_count * 100. / total, total)
    print >> sys.stderr, "Average id = {0:.2f}%".\
            format(100 - (mismatches + gaps) * 100. / alignlen)

    queries_combined = sum(sizes[x] for x in queries)
    print >> sys.stderr, "Coverage: {0} covered, {1} total".\
            format(covered, queries_combined)
    print >> sys.stderr, "Average coverage = {0:.2f}%".\
            format(covered * 100. / queries_combined)

    if opts.ids:
        filename = opts.ids
        fw = must_open(filename, "w")
        for id in valid:
            print >> fw, id
        logging.debug("Queries beyond cutoffs {0} written to `{1}`.".\
                format(cutoff_message, filename))

    outfile = opts.outfile
    if not outfile:
        return

    fp = open(blastfile)
    fw = must_open(outfile, "w")
    blast = Blast(blastfile)
    for b in blast.iter_line():
        if b.query in valid:
            print >> fw, b


def swap(args):
    """
    %prog swap blastfile

    Print out a new blast file with query and subject swapped.
    """
    p = OptionParser(swap.__doc__)

    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    blastfile, = args
    swappedblastfile = blastfile + ".swapped"
    fp = must_open(blastfile)
    fw = must_open(swappedblastfile, "w")
    for row in fp:
        b = BlastLine(row)
        print >> fw, b.swapped

    fw.close()
    sort([swappedblastfile])


def bed(args):
    """
    %prog bed blastfile

    Print out a bed file based on the coordinates in BLAST report.
    """
    p = OptionParser(bed.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    blastfile, = args
    fp = must_open(blastfile)
    bedfile = blastfile.rsplit(".", 1)[0] + ".bed"
    fw = open(bedfile, "w")
    for row in fp:
        b = BlastLine(row)
        print >> fw, b.bedline

    logging.debug("File written to `{0}`.".format(bedfile))

    return bedfile


def set_options_pairs():
    """
    %prog pairs <blastfile|casfile|bedfile|posmapfile>

    Report how many paired ends mapped, avg distance between paired ends, etc.
    Paired reads must have the same prefix, use --rclip to remove trailing
    part, e.g. /1, /2, or .f, .r, default behavior is to truncate until last
    char.
    """
    p = OptionParser(set_options_pairs.__doc__)

    p.add_option("--cutoff", dest="cutoff", default=0, type="int",
            help="distance to call valid links between mates "\
                 "[default: estimate from input]")
    p.add_option("--mateorientation", default=None,
            choices=("++", "--", "+-", "-+"),
            help="use only certain mate orientations [default: %default]")
    p.add_option("--pairsfile", default=None,
            help="write valid pairs to pairsfile [default: %default]")
    p.add_option("--nrows", default=100000, type="int",
            help="only use the first n lines [default: %default]")
    p.add_option("--rclip", default=1, type="int",
            help="pair ID is derived from rstrip N chars [default: %default]")
    p.add_option("--pdf", default=False, action="store_true",
            help="print PDF instead ASCII histogram [default: %default]")
    p.add_option("--bins", default=20, type="int",
            help="number of bins in the histogram [default: %default]")
    p.add_option("--distmode", default="ss", choices=("ss", "ee"),
            help="distance mode between paired reads, ss is outer distance, " \
                 "ee is inner distance [default: %default]")

    return p


def report_pairs(data, cutoff=0, mateorientation=None,
        pairsfile=None, insertsfile=None, rclip=1, ascii=False, bins=20,
        distmode="ss"):
    """
    This subroutine is used by the pairs function in blast.py and cas.py.
    Reports number of fragments and pairs as well as linked pairs
    """
    from jcvi.utils.cbook import percentage

    allowed_mateorientations = ("++", "--", "+-", "-+")

    if mateorientation:
        assert mateorientation in allowed_mateorientations

    num_fragments, num_pairs = 0, 0

    all_dist = []
    linked_dist = []
    # +- (forward-backward) is `innie`, -+ (backward-forward) is `outie`
    orientations = defaultdict(int)

    # clip how many chars from end of the read name to get pair name
    key = (lambda x: x.accn[:-rclip]) if rclip else (lambda x: x.accn)
    data.sort(key=key)

    if pairsfile:
        pairsfw = open(pairsfile, "w")
    if insertsfile:
        insertsfw = open(insertsfile, "w")

    for pe, lines in groupby(data, key=key):
        lines = list(lines)
        if len(lines) != 2:
            num_fragments += len(lines)
            continue

        num_pairs += 1
        a, b = lines

        asubject, astart, astop = a.seqid, a.start, a.end
        bsubject, bstart, bstop = b.seqid, b.start, b.end

        aquery, bquery = a.accn, b.accn
        astrand, bstrand = a.strand, b.strand

        dist, orientation = range_distance(\
                (asubject, astart, astop, astrand),
                (bsubject, bstart, bstop, bstrand),
                distmode=distmode)

        if dist >= 0:
            all_dist.append((dist, orientation, aquery, bquery))

    # select only pairs with certain orientations - e.g. innies, outies, etc.
    if mateorientation:
        all_dist = [x for x in all_dist if x[1] == mateorientation]

    # try to infer cutoff as twice the median until convergence
    if cutoff <= 0:
        dists = np.array([x[0] for x in all_dist], dtype="int")
        p0 = np.median(dists)
        cutoff = int(2 * p0)  # initial estimate
        cutoff = int(math.ceil(cutoff / bins)) * bins
        logging.debug("Insert size cutoff set to {0}, ".format(cutoff) +
            "use '--cutoff' to override")

    for dist, orientation, aquery, bquery in all_dist:
        if dist > cutoff:
            continue

        linked_dist.append(dist)
        if pairsfile:
            print >> pairsfw, "{0}\t{1}\t{2}".format(aquery, bquery, dist)
        orientations[orientation] += 1

    print >>sys.stderr, "%d fragments, %d pairs" % (num_fragments, num_pairs)
    num_links = len(linked_dist)

    linked_dist = np.array(linked_dist, dtype="int")
    linked_dist = np.sort(linked_dist)

    meandist = np.mean(linked_dist)
    stdev = np.std(linked_dist)

    p0 = np.median(linked_dist)
    p1 = linked_dist[int(num_links * .025)]
    p2 = linked_dist[int(num_links * .975)]

    meandist, stdev = int(meandist), int(stdev)
    p0 = int(p0)

    print >>sys.stderr, "%d pairs (%.1f%%) are linked (cutoff=%d)" % \
            (num_links, num_links * 100. / num_pairs, cutoff)

    print >>sys.stderr, "mean distance between mates: {0} +/- {1}".\
            format(meandist, stdev)
    print >>sys.stderr, "median distance between mates: {0}".format(p0)
    print >>sys.stderr, "95% distance range: {0} - {1}".format(p1, p2)
    print >>sys.stderr, "\nOrientations:"

    orientation_summary = []
    for orientation, count in sorted(orientations.items()):
        o = "{0}:{1}".format(orientation, \
                percentage(count, num_links, denominator=False))
        orientation_summary.append(o.split()[0])
        print >>sys.stderr, o

    if insertsfile:
        from jcvi.graphics.histogram import histogram

        print >>insertsfw, "\n".join(str(x) for x in linked_dist)
        insertsfw.close()
        prefix = insertsfile.rsplit(".", 1)[0]
        osummary = " ".join(orientation_summary)
        title="{0} ({1}; median dist:{2})".format(prefix, osummary, p0)
        histogram(insertsfile, vmin=0, vmax=cutoff, bins=bins,
                xlabel="Insertsize", title=title, ascii=ascii)
        if op.exists(insertsfile):
            os.remove(insertsfile)

    return meandist, stdev, p0, p1, p2


def pairs(args):
    """
    See __doc__ for set_options_pairs().
    """
    import jcvi.formats.bed

    p = set_options_pairs()

    opts, targs = p.parse_args(args)

    if len(targs) != 1:
        sys.exit(not p.print_help())

    blastfile, = targs
    bedfile = bed([blastfile])
    args[args.index(blastfile)] = bedfile

    return jcvi.formats.bed.pairs(args)


def best(args):
    """
    %prog best blastfile

    print the best hit for each query in the blastfile
    """
    p = OptionParser(best.__doc__)

    p.add_option("-n", default=1, type="int",
            help="get best N hits [default: %default]")
    p.add_option("--hsps", default=False, action="store_true",
            help="get all HSPs for the best pair [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    sort([blastfile])
    bestblastfile = blastfile + ".best"
    fw = open(bestblastfile, "w")

    b = Blast(blastfile)
    for q, bline in b.iter_best_hit(N=opts.n, hsps=False):
        print >> fw, bline


def summary(args):
    """
    %prog summary blastfile

    Provide summary on id% and cov%, for both query and reference. Often used in
    comparing genomes (based on NUCMER results).
    """
    p = OptionParser(summary.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    qrycovered, refcovered, id_pct = get_stats(blastfile)
    print_stats(qrycovered, refcovered, id_pct)


if __name__ == '__main__':
    main()
