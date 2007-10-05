# -*- coding: utf-8 -*-
'''
File:          tagzilla.py

Authors:       Kevin Jacobs (jacobs@bioinformed.com)
               Zhaoming Wang (wangzha@mail.nih.gov)

Created:       November 8, 2005

Abstract:      A robust and fast SNP binning and tagging program that takes
               many forms of input data, can be tuned by over a dozen
               meaningful parameters, and produces several useful human and
               machine readable outputs.  The heart of the program takes
               genotype data, haplotype frequencies, from which pairwise
               r-squared or D' linkage disequilibrium statistics are
               computed.  Those LD statistics are used to compute bins using
               a greedy maximal algorithm (similar to that of Carlson et al,
               2004), and reports detailed information on bins and tags.
               Many useful extensions are also implemented, including
               sex-linked analysis, efficient multi-population tagging,
               incorporation of design scores and bin informativity
               measures, and calculation of detailed bin and locus coverage
               statistics by type of bin.  Please consult the accompanying
               manual for more information.

Compatibility: Python 2.5 and above

Requires:      No external dependencies, yet...

Revision:      $Id$
'''

__program__   = 'TagZilla'
__authors__   = ['Kevin Jacobs (jacobs@bioinformed.com)',
                 'Zhaoming Wang (wangzha@mail.nih.gov)']
__copyright__ = 'Copyright 2006 Science Applications International Corporation ("SAIC")'
__license__   = 'See GLU license for terms by running: glu license'

__accelerators__ = ['pqueue','tagzillac']

import os
import re
import csv
import sys
import copy
import time
import optparse
import collections

from   itertools import islice, chain, repeat, groupby, izip, dropwhile
from   operator  import attrgetter, itemgetter
from   math      import log, ceil, sqrt, fabs, exp, pi

epsilon = 10e-10

GENO_HEADER = 'rs#\tchr\tpos\t'
HAPMAP_HEADERS = ['rs# SNPalleles chrom pos strand genome_build center protLSID assayLSID panelLSID QC_code',
                  'rs# alleles chrom pos strand assembly# center protLSID assayLSID panelLSID QCcode']
LOCUS_HEADER1 = ['LNAME','LOCATION','MAF','BINNUM','DISPOSITION']
LOCUS_HEADER2 = ['LNAME','LOCATION','POPULATION','MAF','BINNUM','DISPOSITION']
PAIR_HEADER   = ['BIN','LNAME1','LNAME2','POPULATION','RSQUARED','DPRIME','DISPOSITION']
#MULTI_METHODS = ['random', 'merge1', 'merge2', 'merge2+', 'merge3', 'merge3+', 'minld', 'minld+', 'global']
MULTI_METHODS_S = ['merge2', 'merge3', 'minld']
MULTI_METHODS_M = ['global']
MULTI_METHODS = MULTI_METHODS_S + MULTI_METHODS_M
re_spaces = re.compile('[\t ,]+')


class TagZillaError(RuntimeError): pass


def tally(seq):
  '''
  tally(sequence) -> { item:count,... }

  Returns a dictionary of values mapped to the number of times each
  item appears in the input sequence.
  '''
  d = collections.defaultdict(int)
  for item in seq:
    d[item] += 1
  return dict(d)


def slow_count_haplotypes(genos1, genos2):
  '''
    Count the various haplotype combinations and return a vector containing:
       c11 - haplotype counts for allele 1 by allele 1
       c12 - haplotype counts for allele 1 by allele 2
       c21 - haplotype counts for allele 2 by allele 1
       c22 - haplotype counts for allele 2 by allele 2
       dh  - double heterozygote haplotypes (uninformative)
  '''
  if len(genos1) != len(genos2):
    raise ValueError, 'genos1 and genos2 must be of same length'
  diplo_counts = count_diplotypes(genos1, genos2)

  het1,het2 = find_heterozygotes(diplo_counts)
  indices = list(enumerate([(0,0),(0,1),(1,0),(1,1)]))
  x = [0,0,0,0,0]

  for g1,g2,n in diplo_counts:
    if (g1,g2) == (het1,het2):
      x[4] = n
      continue

    if ' ' in g1 and g1[1] != het1[1]:
      g1 = g1[::-1]

    if ' ' in g2 and g2[1] != het2[1]:
      g2 = g2[::-1]

    # Homozygotes count twice, since they appear in only one class --
    # conversely, all other configurations appear in two.
    if ' ' not in g1+g2 and g1 != het1 and g2 != het2:
      n *= 2

    # Sum the counts of each category of allele configurations
    for i,(a,b) in indices:
      if (g1[a],g2[b]) == (het1[a],het2[b]):
        x[i] += n

  return tuple(x)


def count_diplotypes(genos1, genos2):
  '''Return a list of diplotype frequencies and a sets of alleles from each locus'''
  diplo_counts = {}
  for g1,g2 in izip(genos1,genos2):
    if '  ' in (g1,g2):
      continue
    g1 = min(g1)+max(g1)
    g2 = min(g2)+max(g2)
    diplo_counts[g1,g2] = diplo_counts.get( (g1,g2), 0) + 1
  return tuple( (g1,g2,n) for (g1,g2),n in diplo_counts.iteritems() )


def find_heterozygotes(diplo_counts):
  '''Return exemplar heterozygoes for each locus'''
  a1 = set()
  a2 = set()
  for g1,g2,n in diplo_counts:
    a1.update(g1)
    a2.update(g2)

  return find_hetz(a1),find_hetz(a2)


def find_hetz(alleles):
  '''Return the heterozygote genotype for the given alleles'''
  het = ''.join(sorted(alleles)).replace(' ','')
  if len(het) > 2:
    raise ValueError, 'Only biallelic loci are allowed'
  while len(het) < 2:
    het += '\0'
  return het


try:
  # Load the optimized C version of count_haplotypes, if available
  from tagzillac import count_haplotypes

except ImportError:
  # If not, fall back on the pure-Python version
  count_haplotypes = slow_count_haplotypes


def slow_estimate_ld(c11,c12,c21,c22,dh):
  '''
     Compute r-squared (pair-wise) measure of linkage disequlibrium for genotypes at two loci

         c11 - haplotype counts for allele 1 by allele 1
         c12 - haplotype counts for allele 1 by allele 2
         c21 - haplotype counts for allele 2 by allele 1
         c22 - haplotype counts for allele 2 by allele 2
         dh  - double heterozygote haplotypes (uninformative)
  '''

  # Bail out on monomorphic markers
  information = (c11+c12, c21+c22, c11+c21, c12+c22)
  if not dh and 0 in information:
    return 0.,0.

  TOLERANCE = 10e-9

  # Initial estimate
  n = c11 + c12 + c21 + c22 + 2*dh
  p = float(c11 + c12 + dh)/n
  q = float(c11 + c21 + dh)/n

  p11 = p*q
  p12 = p*(1-q)
  p21 = (1-p)*q
  p22 = (1-p)*(1-q)

  loglike = -999999999

  for i in xrange(100):
    oldloglike=loglike

    # Force estimates away from boundaries
    p11=max(epsilon, p11)
    p12=max(epsilon, p12)
    p21=max(epsilon, p21)
    p22=max(epsilon, p22)

    a = p11*p22 + p12*p21

    loglike = (c11*log(p11) + c12*log(p12) + c21*log(p21) + c22*log(p22)
            +  dh*log(a))

    if abs(loglike-oldloglike) < TOLERANCE:
      break

    nx1 = dh*p11*p22/a
    nx2 = dh*p12*p21/a

    p11 = (c11+nx1)/n
    p12 = (c12+nx2)/n
    p21 = (c21+nx2)/n
    p22 = (c22+nx1)/n

  d = p11*p22 - p12*p21

  if d > 0:
    dmax = min( p*(1-q), (1-p)*q )
  else:
    dmax = -min( p*q, (1-p)*(1-q) )

  dprime = d/dmax
  r2 = d*d/(p*(1-p)*q*(1-q))

  return r2,dprime


def slow_bound_ld(c11,c12,c21,c22,dh):
  # Hack to estimate d, maxd, and r2max
  n = c11 + c12 + c21 + c22 + 2*dh
  p = float(c11 + c12 + dh)/n
  q = float(c11 + c21 + dh)/n

  if p and p > 0.5:
    p = 1-p
    c11,c12,c21,c22 = c21,c22,c11,c12
  if q and q > 0.5:
    q = 1-q
    c11,c12,c21,c22 = c12,c11,c22,c21
  if p > q:
    p,q=q,p
    c11,c12,c21,c22 = c22,c21,c12,c11

  # Obtain rough estimate d ignoring double-heterozygotes
  n -= 2*dh
  d = float(c11*c22 - c12*c21)/n/n

  # Distinguish coupling from repulsion:
  #   Magic constant -0.005 can be refined by interval arithmetic, exploiting
  #   the minimum MAF and uncertainty in the estimates of p and q
  if d > -0.005:
    dmax =  min( p*(1-q), (1-p)*q )
  else:
    dmax = -min( p*q, (1-p)*(1-q) )

  if p > 0:
    r2max = dmax*dmax / (p*(1-p)*q*(1-q))
  else:
    r2max = 1.0

  return r2max


try:
  # Load the optimized C version of estimate_ld, if available
  from tagzillac import estimate_ld

except ImportError:
  # If not, fall back on the pure-Python version
  estimate_ld = slow_estimate_ld


def slow_estimate_maf(genos):
  '''
     Estimate Minor Allele Frequency (MAF) for the specified genos

     Missing alleles are coded as ' '
  '''
  f = tally(allele for geno in genos for allele in geno if allele != ' ')
  n = sum(f.itervalues())

  maf = 0
  if len(f) > 2:
    raise ValueError, 'invalid genotypes: locus may have no more than 2 alleles'
  elif len(f) == 2:
    maf = float(min(f.itervalues()))/n

  return maf


try:
  # Load the optimized C version of estimate_maf, if available
  from tagzillac import estimate_maf

except ImportError:
  # If not, fall back on the pure-Python version
  estimate_maf = slow_estimate_maf


def OSGzipFile(filename, mode):
  if "'" in filename or '"' in filename or '\\' in filename:
    raise ValueError, 'Invalid characters in filename'

  if 'w' in mode:
    f = os.popen('/bin/gzip -c > "%s"' % filename, 'w', 10240)
  else:
    f = os.popen('/bin/gunzip -c "%s"' % filename, 'r', 10240)

  return f


def autofile(filename, mode='r', hyphen=None):
  if filename == '-' and hyphen is not None:
    return hyphen

  if filename.endswith('.gz'):
    try:
      f = OSGzipFile(filename, mode)
    except (ImportError,OSError):
      import gzip
      f = gzip.GzipFile(filename, mode)

  else:
    f = file(filename, mode)

  return f


sqrt_pi = sqrt(pi)
log_sqrt_pi = log(sqrt_pi)


def prob_chisq(xx,df):
  '''
  prob_chisq(x,df) => P(X<=x) for a chi-squared distribution

  Returns the (1-tailed) probability value associated with the provided
  chi-square value and df.  Adapted from chisq.c in Gary Perlman's |Stat.
  '''
  BIG = 20.0
  def ex(x):
    if x < -BIG:
      return 0.0
    else:
      return exp(x)

  if xx <=0 or df < 1:
    return 1.0

  a = 0.5 * xx

  even = not df%2

  if df > 1:
    y = ex(-a)

  if even:
    s = y
  else:
    s = 2.0 * prob_z(-sqrt(xx))

  if df > 2:
    xx = 0.5 * (df - 1.0)

    if even:
      z = 1.0
    else:
      z = 0.5

    if a > BIG:
      if even:
        e = 0.0
      else:
        e = log_sqrt_pi

      c = log(a)

      while z <= xx:
        e = log(z) + e
        s = s + ex(c*z-a-e)
        z = z + 1.0

    else:
      if even:
        e = 1.0
      else:
        e = 1.0 / sqrt_pi / sqrt(a)
      c = 0.0
      while z <= xx:
        e = e * a/float(z)
        c = c + e
        z = z + 1.0
      s = c*y+s

  return 1.0-s


def prob_z(z):
  '''
  prob_prob_z(z) => P(Z<=z) for a standard Normal distribution

  Returns the area under the normal curve from -infinity to the given z value.
    for z<0, prob_z(z) = 1-tail probability
    for z>0, 1.0-prob_z(z) = 1-tail probability
    for any z, 2.0*(1.0-prob_z(abs(z))) = 2-tail probability

  Adapted from z.c in Gary Perlman's |Stat.
  '''
  Z_MAX = 6.0  # maximum meaningful z-value
  x = 0.0
  if z != 0.0:
    y = 0.5 * fabs(z)
    if y >= (Z_MAX*0.5):
      x = 1.0
    elif (y < 1.0):
      w = y*y
      x = ((((((((0.000124818987  * w
                 -0.001075204047) * w + 0.005198775019) * w
                 -0.019198292004) * w + 0.059054035642) * w
                 -0.151968751364) * w + 0.319152932694) * w
                 -0.531923007300) * w + 0.797884560593) * y * 2.0
    else:
      y -= 2.0
      x = (((((((((((((-0.000045255659  * y
                       +0.000152529290) * y - 0.000019538132) * y
                       -0.000676904986) * y + 0.001390604284) * y
                       -0.000794620820) * y - 0.002034254874) * y
                       +0.006549791214) * y - 0.010557625006) * y
                       +0.011630447319) * y - 0.009279453341) * y
                       +0.005353579108) * y - 0.002141268741) * y
                       +0.000535310849) * y + 0.999936657524
  if z > 0.0:
    prob = (x+1.0)*0.5
  else:
    prob = (1.0-x)*0.5

  return prob


def count_genos(genos):
  '''
  Estimate allele and genotype frequencies
  Missing alleles are coded as ' '
  '''
  f = tally(g for g in genos if ' ' not in g)

  hom1 = hom2 = het = 0

  for g,n in f.iteritems():
    if g[0] != g[1]:
      het = n
    elif hom1:
      hom2 = n
    else:
      hom1 = n

  return hom1,het,hom2


def hwp_exact_biallelic(hom1_count, het_count, hom2_count):
  '''
  hwp_exact_biallelic(count, het_count, hom2_count):

  Exact SNP test for deviations from Hardy-Weinberg proportions.  Based on
  'A Note on Exact Tests of Hardy-Weinberg Equilibrium', Wigginton JE,
  Cutler DJ and Abecasis GR; Am J Hum Genet (2005) 76: 887-93

  Input: Count of observed homogygote 1, count of observed heterozygotes,
         count of observed homogyhote 2.
  Output: Exact p-value for deviation (2-sided) from Hardy-Weinberg
          Proportions (HWP)

  Complexity: time and space O(min(hom1_count,hom2_count)+het_count)
  '''

  # Computer the number of rare and common alleles
  rare   = 2*min(hom1_count,hom2_count)+het_count
  common = 2*max(hom1_count,hom2_count)+het_count

  # Compute the expected number of heterogygotes under HWP
  hets = rare*common/(rare+common)

  # Account for rounding error on the number of hets, if the
  # parity of rare and hets do not match
  if rare%2 != hets%2:
    hets += 1

  # Initialize the expected number of rare and common homogygotes under HWP
  hom_r = (rare-hets)/2
  hom_c = (common-hets)/2

  # Initialize heterozygote probability vector, such that once filled in
  # P(hets|observed counts) = probs[hets/2]/sum(probs)
  probs = [0]*(rare/2+1)

  # Set P(expected hets)=1, since the remaining probabilities will be
  # computed relative to it
  probs[hets/2] = 1.0

  # Fill in relative probabilities for less than the expected hets
  for i,h in enumerate(xrange(hets,1,-2)):
    probs[h/2-1] = probs[h/2]*h*(h-1) / (4*(hom_r+i+1)*(hom_c+i+1))

  # Fill in relative probabilities fore greater than the expected hets
  for i,h in enumerate(xrange(hets,rare-1,2)):
    probs[h/2+1] = probs[h/2]*4*(hom_r-i)*(hom_c-i) / ((h+1)*(h+2))

  # Compute the pvalue by summing the probabilities <= to that of the
  # observed number of heterogygotes and normalize by the total
  p_obs = probs[het_count/2]
  pvalue = sum(p for p in probs if p <= p_obs)/sum(probs)

  return pvalue


def hwp_chisq_biallelic(hom1_count, het_count, hom2_count):
  '''Return the asymptotic Hardy-Weinberg Chi-squared value and p-value for the given genotypes'''

  n = hom1_count + het_count + hom2_count

  if not n:
    return 1.0

  p = float(2*hom1_count+het_count)/(2*n)
  q = float(2*hom2_count+het_count)/(2*n)

  def score(o,e):
    if e<=0:
      return 0.
    return (o-e)**2/e

  xx = (score(hom1_count,   n*p*p)
     +  score( het_count, 2*n*p*q)
     +  score(hom2_count,   n*q*q))

  return 1-prob_chisq(xx,1)


def hwp_biallelic(genos):
  hom1_count,het_count,hom2_count = count_genos(genos)

  # Only use the exact test when there are less than 1000 rare alleles
  # otherwise, use the asymptotic test
  if 2*min(hom1_count,hom2_count)+het_count < 1000:
    p = hwp_exact_biallelic(hom1_count, het_count, hom2_count)
  else:
    p = hwp_chisq_biallelic(hom1_count, het_count, hom2_count)

  return p


def median(seq):
  '''
  Find the median value of the input sequence
  @param seq: a sequence of numbers
  @type seq: sequence such as list, and it must be able to invoke sort()
  @rtype: number
  @return: the median number for the sequence
  '''
  if not isinstance(seq, list):
    seq = list(seq)
  seq.sort()
  n = len(seq)
  if not n:
    raise ValueError, 'Input sequence cannot be empty'
  if n % 2 == 1:
    return seq[n//2]
  else:
    return (seq[n//2-1]+seq[n//2])/2.0


def average(seq):
  '''
  Find the average value for the input sequence
  @param seq: a sequence of numbers
  @type seq: sequence such as list
  @rtype: float
  @return: the average for the sequence and return 0 if the sequence is empty
  '''
  if not len(seq):
    raise ValueError, 'Input sequence cannot be empty '
  return float(sum(seq))/len(seq)


class Locus(object):
  __slots__ = ('name','location','maf','genos')
  def __init__(self, name, location, genos):
    self.name     = name
    self.location = location
    self.maf      = estimate_maf(genos)
    self.genos    = genos


def scan_ldpairs(loci, maxd, rthreshold, dthreshold):
  '''
     A generator for pairs of loci within a specified genomic distance.
     Loci are assumed to be sorted by genomic location.
  '''
  # Scan each locus
  n = len(loci)
  for i in xrange(n):
    locus1 = loci[i]
    location1 = locus1.location

    # And up to maxd distance beyond it
    for j in xrange(i+1,n):
      locus2 = loci[j]
      location2 = locus2.location

      if location2 - location1 > maxd:
        break

      counts = count_haplotypes(locus1.genos, locus2.genos)
      r2,dprime = estimate_ld(*counts)

      if r2 >= rthreshold and abs(dprime) >= dthreshold:
        yield locus1.name,locus2.name,r2,dprime


def merge_multi_loci(loci):
  def locus_iter(loci):
    return chain(loci,repeat(None))

  pops  = len(loci)
  loci  = [ locus_iter(locus) for locus in loci ]
  locus = [ l.next() for l in loci ]

  while 1:
    if locus.count(None) == pops:
      break

    location,name,min_locus = min( (l.location,l.name,l) for l in locus if l )

    results = []
    for i,l in enumerate(locus):
      if l and l.name == name and l.location == location:
        results.append(l)
        locus[i] = loci[i].next()
      else:
        results.append( Locus(name, location, []) )

    yield results


def fill_empty(genos, n):
  m = len(genos)
  assert m <= n
  if m < n:
    genos.extend( ['  ']*(n-m) )
  return genos


def merge_loci(loci):
  loci = list(merge_multi_loci(loci))

  # FIXME: Materializing full streams is somewhat wasteful.  We should use
  #        itertools.tee instead.  However, this is the safer method until
  #        the load_genotypes routines are augmented to always return a
  #        fixed number of genotypes.
  pops = len(loci[0])
  lens = [0]*pops
  for locus in loci:
    lens = [ max(m,len(l.genos)) for m,l in izip(lens,locus) ]

  assert len(lens) == pops

  for locus in loci:
    assert len(locus) == pops
    genos = []
    for i in xrange(pops):
      genos.extend(fill_empty(locus[i].genos, lens[i]))

    try:
      yield Locus(locus[0].name, locus[0].location, genos)
    except ValueError:
      print >> sys.stderr, 'WARNING: BAD LOCUS %s (too many alleles).  Skipping.' % locus[0].name


def scan_ldpairs_multi(loci, maxd, multi_options):
  '''
     A generator for pairs of loci within a specified genomic distance.
     Loci are assumed to be sorted by genomic location.
  '''

  loci = list(merge_multi_loci(loci))
  ths = [ (options.d,options.r) for options in multi_options ]

  # Scan each locus
  n = len(loci)
  for i in xrange(n):
    location1 = loci[i][0].location

    # And up to maxd distance beyond it
    for j in xrange(i+1,n):
      location2 = loci[j][0].location

      if location2 - location1 > maxd:
        break

      # Initialize r2 and dprime to absurdly large values, since we will be
      # tracking their minimums
      r2 = dprime = 10

      # Track if we have seen only pairs of loci that meet the required
      # thresholds
      good = True

      for (dth,rth),locus1,locus2 in izip(ths,loci[i],loci[j]):
        if not locus1.genos or not locus2.genos:
          continue

        counts = count_haplotypes(locus1.genos, locus2.genos)
        r2_pop,dprime_pop = estimate_ld(*counts)

        r2,dprime = min(r2,r2_pop),min(dprime,dprime_pop)

        # Check the population-specific thresholds and stop if the current
        # pair do not meet them.
        if r2_pop < rth or abs(dprime_pop) < dth:
          good = False
          break

      # If there was at least one valid pair of loci (r2<10) and all pairs
      # met the necessary thresholds, yield the locus names and
      # corresponding minimum r-squared and dprime
      if r2<10 and good:
        yield locus1.name,locus2.name,r2,dprime


def filter_loci_by_maf(loci, minmaf, minobmaf, include):
  '''
     Generator that filters loci by a minimum MAF

     Loci come in two flavors, each with a distinct minimum MAF.
     If the locus.name is not in the provided include set, then
     the minmaf parameter is used as a threshold.  Otherwise, the
     minobmaf (minimum obligate MAF) threshold is applied.
  '''

  mafs = (minmaf,minobmaf)
  for locus in loci:
    # For mafs[locus.name in include], the index evaluatates to:
    #    False == 0: Choose mafs[0] == minmaf
    #    True  == 1: Choose mafs[1] == minobmaf
    if locus.maf >= mafs[locus.name in include]:
      yield locus


def filter_loci_by_inclusion(loci, include):
  '''Generator that filters loci based on an inclusion set'''

  for locus in loci:
    if locus.name in include:
      yield locus


def filter_loci_by_hwp(loci, pvalue):
  '''
  Generator that filters loci based on significance of deviation from
  Hardy-Weinberg proportions
  '''
  for locus in loci:
    p = hwp_biallelic(locus.genos)
    if p >= pvalue:
      yield locus


range_all = (-sys.maxint,sys.maxint)

def filter_loci_by_range(loci, rangestring):
  '''Generator that filters loci based on an inclusion range'''

  ranges = []
  for range in rangestring.split(','):
    try:
      start,stop = range.split('-')
      start = int(start or -sys.maxint)
      stop  = int(stop  or  sys.maxint)

      if stop < start:
        start,stop = stop,start

    except (ValueError,TypeError):
      raise TagZillaError,'ERROR: Invalid genomic range: %s' % range

    ranges.append( (start,stop) )

  if range_all in ranges:
    ranges = [range_all]

  for locus in loci:
    for start,stop in ranges:
      if start <= locus.location < stop:
        yield locus
        break


def completion(locus):
  return len(locus) - locus.count('  '),len(locus)


def filter_loci_by_completion(loci, mincompletion, mincompletionrate):
  '''Generator that filters loci by a minimum completion rate'''

  for locus in loci:
    m,n = completion(locus.genos)

    rate = 0
    if n:
      rate = float(m)/n

    if m >= mincompletion and rate >= mincompletionrate:
      yield locus


def pair_generator(bins):
  '''Generator for unique pairs of bins'''

  if not isinstance(bins, (list,tuple)):
    bins = list(bins)
  n = len(bins)
  for i in xrange(n):
    for j in xrange(0,i):
      yield bins[i],bins[j]


class Bin(set):
  INCLUDE_UNTYPED = -2
  INCLUDE_TYPED   = -1
  NORMAL          =  0
  EXCLUDE         =  2

  __slots__ = ('maf','disposition','maxcovered')
  def __init__(self, iterable=None, maf=None, disposition=NORMAL, maxcovered=None):
    if iterable is not None:
      set.__init__(self,iterable)
    else:
      set.__init__(self)
    self.maf = maf or 0.
    self.disposition = disposition
    self.maxcovered = max(maxcovered,len(self))

  def add(self, lname, maf):
    set.add(self, lname)
    self.maxcovered = max(self.maxcovered,len(self))
    self.maf += maf

  def remove(self, lname, maf):
    set.remove(self, lname)
    self.maf -= maf

  def discard(self, lname, maf):
    if lname in self:
      set.remove(self, lname)
      self.maf -= maf

  def average_maf(self):
    return float(self.maf)/len(self)

  def priority(self):
    return (self.disposition, -len(self), -self.maf)

  def __reduce__(self):
    return (Bin,(list(self),self.maf,self.disposition,self.maxcovered))

  def __repr__(self):
    return 'Bin(%s,%f,%d,%d)' % (list(self),self.maf,self.disposition,self.maxcovered)


def binldcmp(x,y):
  if x[0] == x[1] or y[0] == y[1]:
    return 1
  return -cmp(x[2],y[2]) or cmp(x[0],y[0]) or cmp(x[1],y[1])


result_priority_map = { 'obligate-untyped'  : -2,
                        'obligate-typed'    : -1,
                        'maximal-bin'       :  0,
                        'residual'          :  1,
                        'obligate-exclude'  :  2 }


class BinResult(object):
  __slots__ = ('binnum','tags','others','tags_required','average_maf','include',
               'ld','disposition','maxcovered','recommended_tags','include_typed')

  def sort(self):
    self.ld.sort(cmp=binldcmp)

  def priority(self):
    return (result_priority_map[self.disposition], -len(self), -self.average_maf)

  def __len__(self):
    return len(self.tags) + len(self.others)

  def __iter__(self):
    return chain(self.tags,self.others)

  def __le__(self, other):
    return self.priority() <= other.priority()


class BinStat(object):
  def __init__(self):
    self.count         = 0
    self.tags_required = 0
    self.loci          = 0
    self.width         = 0
    self.spacing       = 0
    self.total_tags    = 0
    self.others        = 0
    self.includes      = 0
    self.excludes      = 0

  def update(self, required, tags, others, width, spacing, include, excludes):
    self.count         += 1
    self.tags_required += required
    self.loci          += tags + others
    self.width         += width
    self.spacing       += spacing
    self.total_tags    += tags
    self.others        += others
    if include:
      self.includes += 1
    self.excludes += excludes

  def __add__(self, other):
    new = BinStat()
    new.count         = self.count         + other.count
    new.tags_required = self.tags_required + other.tags_required
    new.loci          = self.loci          + other.loci
    new.width         = self.width         + other.width
    new.spacing       = self.spacing       + other.spacing
    new.total_tags    = self.total_tags    + other.total_tags
    new.others        = self.others        + other.others
    new.includes      = self.includes      + other.includes
    new.excludes      = self.excludes      + other.excludes
    return new


class NullPairwiseBinOutput(object):
  def emit_bin(self, bin, qualifier, population, options):
    pass

  def emit_extra(self, lddata, tags, population):
    pass


class PairwiseBinOutput(NullPairwiseBinOutput):
  def __init__(self, outfile, exclude):
    self.outfile = outfile
    self.exclude = exclude
    outfile.write('BIN\tLNAME1\tLNAME2\tPOPULATION\tRSQUARED\tDPRIME\tDISPOSITION\n')

  def emit_bin(self, bin, qualifier, population, options):
    outfile = self.outfile
    exclude = self.exclude
    bin.sort()

    for lname1,lname2,r2,dprime in bin.ld:
      if options.skip and (bin.disposition in ('obligate-exclude','residual')
                        or lname1 in exclude or lname2 in exclude):
        continue

      r2 = sfloat(r2)
      dprime = sfloat(dprime)
      disposition = pair_disposition(lname1, lname2, bin, qualifier)
      outfile.write('%d\t%s\t%s\t%s\t%s\t%s\t%s\n' % (bin.binnum,lname1,lname2,population,r2,dprime,disposition))

  def emit_extra(self, lddata, tags, population):
    outfile = self.outfile
    bin = BinResult()
    bin.tags = set(tags)
    for (lname1,lname2),(r2,dprime) in lddata.iteritems():
      disposition = pair_disposition(lname1,lname2,bin,qualifier='interbin')
      r2     = sfloat(r2)
      dprime = sfloat(dprime)
      outfile.write('\t%s\t%s\t%s\t%s\t%s\t%s\n' % (lname1,lname2,population,r2,dprime,disposition))


def save_ldpairs(filename, ldpairs):
  out = csv.writer(autofile(filename,'w'),dialect='excel-tab')
  out.writerow(['LNAME1','LNAME2','RSQUARED','DPRIME'])

  def _gen():
    for pairs in ldpairs:
      for p in pairs:
        out.writerow(p)
        yield p

  return [_gen()]


class NullLocusOutput(object):
  def emit_bin(self, bin, locusmap, qualifier, population):
    pass


class LocusOutput(NullLocusOutput):
  def __init__(self, locusinfofile, exclude):
    self.locusinfofile = locusinfofile
    self.exclude = exclude
    locusinfofile.write('LNAME\tLOCATION\tPOPULATION\tMAF\tBINNUM\tDISPOSITION\n')

  def emit_bin(self, bin, locusmap, qualifier, population):
    locusinfofile = self.locusinfofile
    exclude = self.exclude
    for lname in chain(bin.tags,bin.others):
      disposition = locus_disposition(lname, bin, exclude, qualifier)
      l = locusmap[lname]
      maf = sfloat(l.maf)
      locusinfofile.write('%s\t%d\t%s\t%s\t%d\t%s\n' % (l.name, l.location, population, maf, bin.binnum, disposition))


class NullBinInfo(object):
  def emit_bin(self, bin, loci, exclude, population):
    pass

  def emit_summary(self, sumfile, population):
    pass

  def emit_multipop_summary(self, sumfile, ptags):
    pass


class BinInfo(NullBinInfo):
  dispositions = ['obligate-untyped','obligate-typed','maximal-bin','residual','obligate-exclude']

  def __init__(self, outfile, histomax):
    self.outfile = outfile
    self.stats = {}
    self.histomax = histomax

  def emit_bin(self, bin, loci, exclude, population):
    out = self.outfile

    binnum  = bin.binnum
    binsize = len(bin)
    amaf    = bin.average_maf*100
    locs    = sorted([ loci[lname].location for lname in bin ])
    spacing = sorted([ locs[i+1]-locs[i] for i in xrange(len(locs)-1) ])
    width   = locs[-1]-locs[0]
    excls = exclude.intersection(bin)

    aspacing = 0
    if len(spacing) > 1:
      aspacing = average(spacing)

    if bin.maxcovered == 1:
      hlen = 0
    else:
      hlen = min(self.histomax,binsize)

    stats = self.stats.get(population,None)
    if stats is None:
      stats = self.stats[population] = {}

    d = bin.disposition
    if d not in stats:
      stats[d] = [ BinStat() for i in xrange(self.histomax+1) ]

    stats[d][hlen].update(bin.tags_required, len(bin.tags), len(bin.others),
                          width, aspacing, bin.include is not None, len(excls))

    if not out:
      return

    population = population or 'user specified'
    out.write('Bin %-4d population: %s, sites: %d, tags %d, other %d, tags required %d, width %d, avg. MAF %.1f%%\n' \
                   % (binnum,population,binsize,len(bin.tags),len(bin.others),bin.tags_required,width,amaf))
    out.write('Bin %-4d Location: min %d, median %d, average %d, max %d\n' \
                  % (binnum,locs[0],median(locs),average(locs),locs[-1]))
    if len(spacing) > 1:
      out.write('Bin %-4d Spacing: min %d, median %d, average %d, max %d\n' \
                    % (binnum,spacing[0],median(spacing),average(spacing),spacing[-1]))
    out.write('Bin %-4d TagSnps: %s\n' % (binnum,' '.join(sorted(bin.tags))))
    if bin.recommended_tags:
      out.write('Bin %-4d RecommendedTags: %s\n' % (binnum, ' '.join(bin.recommended_tags)))
    out.write('Bin %-4d other_snps: %s\n' % (binnum,' '.join(sorted(bin.others))))

    if bin.include is not None:
      if bin.disposition == 'obligate-untyped':
        out.write('Bin %-4d Obligate_tag: %s, untyped\n' % (binnum,bin.include))
      else:
        out.write('Bin %-4d Obligate_tag: %s, typed\n' % (binnum,bin.include))

    if excls:
      out.write('Bin %-4d Excluded_as_tags: %s\n' % (binnum,' '.join(sorted(excls))))

    out.write('Bin %-4d Bin_disposition: %s\n' % (binnum,bin.disposition))
    out.write('Bin %-4d Loci_covered: %s\n' % (binnum,bin.maxcovered))
    out.write('\n')


  def emit_summary(self, sumfile, population):
    out = sumfile
    stats = self.stats.get(population,{})

    tstats = {}
    for d in self.dispositions:
      if d in stats:
        self.emit_summary_stats(out, stats[d], d, population)
        tstats[d] = sum(stats[d], BinStat())

    if not population:
      out.write('\nBin statistics by disposition:\n')
    else:
      out.write('\nBin statistics by disposition for population %s:\n' % population)

    out.write('                      tags                                total   non-     avg    avg\n')
    out.write(' disposition          req.   bins     %    loci      %    tags    tags    tags  width\n')
    out.write(' -------------------- ------ ------ ------ ------- ------ ------- ------- ---- ------\n')

    total_bins = sum(s.count for s in tstats.values())
    total_loci = sum(s.loci  for s in tstats.values())

    for d in self.dispositions:
      self.emit_summary_line(out, '%-20s' % d, tstats.get(d,BinStat()), total_bins, total_loci)

    self.emit_summary_line(out, '              Total ', sum(tstats.values(), BinStat()), total_bins, total_loci)
    out.write('\n')
    out.flush()


  def emit_multipop_summary(self, sumfile, tags):
    n = sum(tags.itervalues())

    sumfile.write('\nTags required by disposition for all population:\n')

    sumfile.write('                      tags         \n')
    sumfile.write(' disposition          req.     %   \n')
    sumfile.write(' -------------------- ------ ------\n')

    for d in self.dispositions:
      m = tags.get(d,0)
      sumfile.write(' %-20s %6d %6.2f\n' % (d,m,percent(m,n)))

    sumfile.write('              Total   %6d %6.2f\n\n' % (n, 100))
    sumfile.flush()


  def emit_summary_stats(self, out, stats, disposition, population):
    if not population:
      out.write('\nBin statistics by bin size for %s:\n\n' % disposition)
    else:
      out.write('\nBin statistics by bin size for %s in population %s:\n\n' % (disposition,population))

    out.write(' bin   tags                                total   non-     avg    avg\n')
    out.write(' size  req.   bins     %    loci      %    tags    tags    tags  width\n')
    out.write(' ----- ------ ------ ------ ------- ------ ------- ------- ---- ------\n')
    total_bins = sum(s.count for s in stats)
    total_loci = sum(s.loci  for s in stats)

    hlist = [ i for i,s in enumerate(stats) if s.count ]
    hmin = min(hlist)
    hmax = max(hlist)

    for i in xrange(hmin,hmax+1):
      if not i:
        label = 'singl'
      elif i == self.histomax:
        label = '>%2d  ' % (i-1)
      else:
        label = '%3d  ' % i

      self.emit_summary_line(out, label, stats[i], total_bins, total_loci)

    self.emit_summary_line(out, 'Total', sum(stats, BinStat()), total_bins, total_loci)
    out.write('\n')


  def emit_summary_line(self, out, label, stats, total_bins, total_loci):
    n = stats.count
    m = stats.loci
    if n:
      t = float(stats.total_tags) / n
      w = float(stats.width) / n
    else:
      t,w = 0,0
    out.write(' %s %6d %6d %6.2f %7d %6.2f %7d %7d %4.1f %6d\n' % (label,
              stats.tags_required,n,percent(n,total_bins),
              m,percent(m,total_loci),stats.total_tags,stats.others,t,w))


def locus_result_sequence(filename, locusmap, exclude):
  '''
  Returns a generator of BinResult objects for the tagzilla locus ouput
  file name.

  The locusmap dictionary and the exclude set are filled in incrementally as
  the result stream is processed.

  NOTE: This function is not currently used by tagzilla -- rather it exists
        to unparse tagzilla output and is included as a utility function
        for when tagzilla is used as a module.
  '''
  import csv

  locusfile = csv.reader(autofile(filename), dialect='excel-tab')

  header = locusfile.next()

  if header == PAIR_HEADER:
    version = 0
    grouper = lambda row: (row[0],row[3])
  elif header == LOCUS_HEADER1:
    version = 1
    grouper = itemgetter(3)
  elif header == LOCUS_HEADER2:
    version = 2
    grouper = lambda row: (row[4],row[2])
  else:
    raise TagZillaError, 'ERROR: Invalid input format for file %s.' % filename


  for binnum,(_,loci) in enumerate(groupby(locusfile,grouper)):
    bin = BinResult()
    bin.binnum = binnum

    bin.tags = []
    bin.others = []
    bin.ld = []
    bin.include = None
    bin.average_maf = 0
    bin.maxcovered = 0
    bin.recommended_tags = []
    bin.disposition = 'maximal-bin'
    bin.tags_required = 1

    for locus in loci:
      if version == 0:
        if locus[1] != locus[2]:
          continue
        lname = locus[1]
        location = 0
        population = locus[3]
        maf = 0.5
        disposition = locus[6]
      elif version == 1:
        lname,location,maf,binnum,disposition = locus
        population = ''
      else:
        lname,location,population,maf,binnum,disposition = locus

      bin.binnum = binnum
      locus = locusmap[lname] = Locus(lname, int(location), [])

      maf = float(maf)
      locus.maf = maf
      bin.average_maf += maf

      disposition = disposition.split(',')

      if 'other' in disposition:
        bin.others.append(lname)
      elif 'exclude' in disposition:
        bin.others.append(lname)
        exclude.add(lname)
      elif 'excluded-tag' in disposition:
        bin.tags.append(lname)
        bin.disposition = 'obligate-exclude'
      elif 'obligate-tag' in disposition:
        bin.tags.append(lname)
        bin.disposition = 'obligate-include'
        bin.include = lname
      elif 'untyped-tag' in disposition:
        bin.tags.append(lname)
        bin.disposition = 'obligate-untyped'
        bin.include = lname
      elif 'typed-tag' in disposition:
        bin.tags.append(lname)
        bin.disposition = 'obligate-typed'
        bin.include = lname
      elif 'alternate-tag' in disposition:
        bin.tags.append(lname)
      elif 'candidate-tag' in disposition or \
           'necessary-tag' in disposition:
        bin.tags.append(lname)
      elif 'lonely-tag' in disposition:
        bin.tags.append(lname)
        bin.maxcovered = 2
      elif 'singleton-tag' in disposition:
        bin.tags.append(lname)

      if 'recommended' in disposition:
        bin.recommended_tags.append(lname)

      if 'untyped_bin' in disposition:
        bin.disposition = 'obligate-untyped'

      if 'typed_bin' in disposition:
        bin.disposition = 'obligate-typed'

      if 'residual' in disposition:
        bin.disposition = 'residual'

    bin.maxcovered = max(bin.maxcovered,len(bin))
    bin.average_maf /= len(bin)

    yield population,bin


def must_split_bin(bin, binsets, get_tags_required):
  if not get_tags_required:
    return False

  tags_required = get_tags_required(len(bin))

  if tags_required == 1:
    return False

  tags = [ lname for lname in bin if can_tag(binsets[lname],bin) ]

  return len(tags) < tags_required <= len(bin)


class NaiveBinSequence(object):
  def __init__(self, loci, binsets, lddata, get_tags_required):
    self.loci = loci
    self.binsets = binsets
    self.lddata = lddata
    self.get_tags_required = get_tags_required

  def __iter__(self):
    return self

  def pop(self):
    if not self.binsets:
      raise StopIteration

    while 1:
      ref_lname = self.peek()
      largest = self.binsets[ref_lname]

      if not must_split_bin(largest, self.binsets, self.get_tags_required):
        break

      self.split_bin(ref_lname,largest)

    # Remove all references to this locus from any other
    # binset not captured
    bins = {}
    for lname in largest:
      bin = self.pop_bin(lname)
      bins[lname] = bin
      maf = self.loci[lname].maf
      for lname2 in bin - largest:
        self.reduce_bin(lname2, lname, maf)

    return ref_lname,largest,bins

  next = pop

  def peek(self):
    '''
       Find the largest bin among all the sets, selecting bins ordered by
       inclusion status, size (descending), and MAF (descending).  This
       implementation is mainly for demonstration and testing, as it uses a
       naive and potentially very slow linear search.  See the
       FastBinSequence descendant class for a more efficient solution based
       on a priority queue.
    '''
    bins = self.binsets.iteritems()
    # First set the first bin as the best
    lname, bin = bins.next()
    prio = bin.priority()
    # Then iterate through the remaining items to refine the result
    for current_lname,bin in bins:
      current_prio = bin.priority()
      if current_prio < prio:
        lname = current_lname
        prio = current_prio

    return lname

  def pop_bin(self, lname):
    return self.binsets.pop(lname)

  def reduce_bin(self, other_locus, taken_locus, maf):
    if other_locus in self.binsets:
      self.binsets[other_locus].discard(taken_locus, maf)

  def split_bin(self, ref_lname, bin):
    ld = []
    for lname in bin:
      if lname == ref_lname:
        continue

      covered = len(self.binsets.get(lname,[]))

      lname1,lname2 = ref_lname,lname
      if (lname1,lname2) not in self.lddata:
        lname1,lname2=lname2,lname1

      r2,dprime = self.lddata[lname1,lname2]
      ld.append( (-covered,r2,lname) )

    ld.sort()

    # Remove smallest ld value
    covered,r2,lname = ld[0]
    self.reduce_bin(ref_lname, lname, self.loci[lname].maf)
    self.reduce_bin(lname, ref_lname, self.loci[ref_lname].maf)


class FastBinSequence(NaiveBinSequence):
  def __init__(self, loci, binsets, lddata, get_tags_required):
    NaiveBinSequence.__init__(self, loci, binsets, lddata, get_tags_required)

    import pqueue
    self.pq = pq = pqueue.PQueue()

    for lname,bin in binsets.iteritems():
      pq[lname] = bin.priority()

  def peek(self):
    priority,ref_lname = self.pq.peek()
    return ref_lname

  def pop_bin(self, lname):
    del self.pq[lname]
    return NaiveBinSequence.pop_bin(self, lname)

  def reduce_bin(self, other_locus, taken_locus, maf):
    NaiveBinSequence.reduce_bin(self, other_locus, taken_locus, maf)
    if other_locus in self.binsets:
      self.pq[other_locus] = self.binsets[other_locus].priority()


def BinSequence(loci, binsets, lddata, get_tags_required):
  try:
    return FastBinSequence(loci, binsets, lddata, get_tags_required)
  except ImportError:
    pass

  return NaiveBinSequence(loci, binsets, lddata, get_tags_required)


class NaiveMultiBinSequence(object):
  def __init__(self, loci, binsets, lddata, get_tags_required):
    self.loci    = loci
    self.binsets = binsets
    self.lddata  = lddata
    self.get_tags_required = get_tags_required

    self.lnames = set()
    for pop_binsets in binsets:
      self.lnames.update(pop_binsets)

  def __iter__(self):
    return self

  def pop(self):
    if not any(self.binsets):
      raise StopIteration

    while 1:
      ref_lname = self.peek()

      split = False
      for pop_binsets,pop_loci in izip(self.binsets,self.loci):
        bin = pop_binsets.get(ref_lname,None)
        if bin and must_split_bin(bin, pop_binsets, self.get_tags_required):
          self.split_bin(pop_binsets, pop_loci, ref_lname,bin)
          split = True
          break

      if not split:
        break

    # Remove all references to this locus from any other
    # binset not captured
    largest = []
    bins = []
    touched = set()
    for pop_binsets,pop_loci in izip(self.binsets,self.loci):
      used_bins = {}
      lbin = pop_binsets.get(ref_lname,None)
      if lbin:
        touched.update(lbin)
        for lname in lbin:
          bin = pop_binsets.pop(lname)
          used_bins[lname] = bin
          maf = pop_loci[lname].maf
          for lname2 in bin - lbin:
            self.reduce_bin(pop_binsets, lname2, lname, maf)

      largest.append(lbin)
      bins.append(used_bins)

    self.update_bins(touched)
    return ref_lname,largest,bins

  next = pop

  def peek(self):
    lnames = iter(self.lnames)

    prio = None
    while prio is None:
      lname = lnames.next()
      prio = self.priority(lname)

    # Then iterate through the remaining items to refine the result
    for lname in lnames:
      current_prio = self.priority(lname)
      if current_prio is not None and current_prio < prio:
        lname = current_lname
        prio = current_prio

    return lname

  def reduce_bin(self, binsets, other_locus, taken_locus, maf):
    if other_locus in binsets:
      binsets[other_locus].discard(taken_locus, maf)

  def priority(self, lname):
    disposition = 1000
    binlen = maf = pops = 0
    minlen = sys.maxint

    for pop_binsets in self.binsets:
      bin = pop_binsets.get(lname)
      if bin:
        disposition = min(disposition, bin.disposition)
        minlen  = min(minlen,len(bin))
        binlen += len(bin)
        maf    += bin.maf
        pops   += 1

    if minlen == 1:
      pops   *= 2
      binlen *= 2

    if disposition < 1000:
      return (disposition,-pops,-binlen,-maf)
    else:
      return None

  def split_bin(self, binsets, loci, ref_lname, bin):
    ld = []
    for lname in bin:
      if lname == ref_lname:
        continue

      covered = len(binsets.get(lname,[]))

      lname1,lname2 = ref_lname,lname
      if (lname1,lname2) not in self.lddata:
        lname1,lname2=lname2,lname1

      r2,dprime = self.lddata[lname1,lname2]
      ld.append( (-covered,r2,lname) )

    ld.sort()

    # Remove smallest ld value
    covered,r2,lname = ld[0]
    self.reduce_bin(binsets, ref_lname, lname, loci[lname].maf)
    self.reduce_bin(binsets, lname, ref_lname, loci[ref_lname].maf)
    self.update_bins([ref_lname,lname])

  def update_bins(self,lnames):
    pass


class FastMultiBinSequence(NaiveMultiBinSequence):
  def __init__(self, loci, binsets, lddata, get_tags_required):
    NaiveMultiBinSequence.__init__(self, loci, binsets, lddata, get_tags_required)

    import pqueue
    self.pq = pq = pqueue.PQueue()

    for lname in self.lnames:
      pq[lname] = self.priority(lname)

  def peek(self):
    priority,ref_lname = self.pq.peek()
    return ref_lname

  def update_bins(self,lnames):
    for lname in lnames:
      p = self.priority(lname)
      if p is not None:
        self.pq[lname] = p
      else:
        del self.pq[lname]


def MultiBinSequence(loci, binsets, lddata, get_tags_required):
  try:
    return FastMultiBinSequence(loci, binsets, lddata, get_tags_required)
  except ImportError:
    pass

  return NaiveMultiBinSequence(loci, binsets, lddata, get_tags_required)


def build_binsets(loci, ldpairs, includes, exclude, designscores):
  '''
  Build initial data structures:
    binsets: Dictionary that for each locus, stores the set of all other
             loci that meet the rthreshold and the sum of the MAFs
    lddata:  Dictionary of locus pairs to r-squared values
  '''

  binsets = {}
  lddata  = {}

  for pairs in ldpairs:
    for lname1,lname2,r2,dprime in pairs:
      if lname1 not in binsets:
        binsets[lname1] = Bin([lname1], loci[lname1].maf)
      if lname2 not in binsets:
        binsets[lname2] = Bin([lname2], loci[lname2].maf)

      lddata[lname1,lname2] = r2,dprime
      binsets[lname1].add(lname2, loci[lname2].maf)
      binsets[lname2].add(lname1, loci[lname1].maf)

  # Add singletons once all ldpair sets have been consumed.
  # This is necessary since loci are filled in lazily
  for lname in loci:
    if lname not in binsets:
      binsets[lname] = Bin([lname], loci[lname].maf)

  # Update the bin disposition if the lname is one of the excludes
  for lname in exclude:
    if lname in binsets:
      binsets[lname].disposition = Bin.EXCLUDE

  # Update the bin disposition for all undesignable loci, if design scores
  # are provided
  if designscores:
    for lname,bin in binsets.iteritems():
      if designscores.get(lname,0) < epsilon:
        bin.disposition = bin.EXCLUDE
        exclude.add(lname)

  # Build include sets and pre-remove obligates from other include bins
  for lname in includes.untyped:
    if lname in binsets:
      bin = binsets[lname]
      bin.disposition = Bin.INCLUDE_UNTYPED

      for lname2 in includes.untyped & bin:
        if lname != lname2:
          binsets[lname].remove(lname2, loci[lname2].maf)

  for lname in includes.typed:
    if lname in binsets:
      bin = binsets[lname]
      bin.disposition = Bin.INCLUDE_TYPED

  return binsets,lddata


def bin_qualifier(bin, binned_loci, options):
  qualifier = ''
  if ((options.targetbins and  bin.binnum > options.targetbins) or \
      (options.targetloci and binned_loci > options.targetloci)) and \
      bin.disposition != 'obligate-exclude':
    qualifier = 'residual'
    bin.disposition = 'residual'
  elif bin.disposition == 'obligate-exclude':
    qualifier = 'excluded'
  elif bin.disposition == 'obligate-typed':
    qualifier = 'typed_bin'
  elif bin.disposition == 'obligate-untyped':
    qualifier = 'untyped_bin'
  return qualifier


def tag_disposition(lname, bin):
  if bin.disposition == 'obligate-untyped':
    if lname == bin.include:
      disposition = 'untyped-tag'
    elif lname in bin.include_typed:
      disposition = 'redundant-tag'
    else:
      disposition = 'alternate-tag'
  elif bin.disposition == 'obligate-typed':
    if lname == bin.include:
      disposition = 'typed-tag'
    elif lname in bin.include_typed:
      disposition = 'redundant-tag'
    else:
      disposition = 'alternate-tag'
  elif bin.disposition == 'obligate-exclude':
    disposition = 'excluded-tag'
  elif len(bin.tags) > 1:
    disposition = 'candidate-tag'
  elif len(bin) > 1:
    disposition = 'necessary-tag'
  elif bin.maxcovered > 1:
    disposition = 'lonely-tag'
  else:
    disposition = 'singleton-tag'

  if lname in bin.recommended_tags:
    disposition += ',recommended'

  return disposition


def locus_disposition(lname, bin, exclude, qualifier=None):
  if lname in bin.tags:
    disposition = tag_disposition(lname, bin)
  elif lname in exclude and bin.disposition != 'obligate-exclude':
    disposition = 'exclude'
  else:
    disposition = 'other'

  if qualifier:
    disposition = '%s,%s' % (disposition,qualifier)

  return disposition


def pair_disposition(lname1, lname2, bin, qualifier=None):
  if lname1 == lname2:
    disposition = tag_disposition(lname1, bin)
  else:
    labels = ['other','tag']
    # Don't ask -- you really don't want to know.  Sigh.
    disposition = '%s-%s' % (labels[lname1 in bin.tags], labels[lname2 in bin.tags])

  if qualifier:
    disposition = '%s,%s' % (disposition,qualifier)

  return disposition


def sfloat(n):
  '''Compactly format a float as a string'''
  return ('%.3f' % n).rstrip('0.').lstrip('0') or '0'


def percent(a,b):
  if not b:
    return 0.
  return float(a)/b*100


def can_tag(bin, reference):
  '''
  Return True if the specified candidate bin can tag the reference bin, False otherwise.

  The following conditions must hold:
    1) If the reference bin disposition is not an exclude, then the candidate bin cannot be either.
    2) The contents of the candidate bin set must be a superset of the reference bin.
  '''
  return (bin.disposition != Bin.EXCLUDE or reference.disposition == Bin.EXCLUDE) \
         and bin.issuperset(reference)


def build_result(lname, largest, bins, lddata, includes, get_tags_required):
  result = BinResult()

  if get_tags_required:
    result.tags_required = get_tags_required(len(largest))
  else:
    result.tags_required = 1

  result.recommended_tags = []
  result.include_typed    = includes.typed & largest
  result.average_maf = largest.average_maf()
  result.maxcovered  = largest.maxcovered

  if largest.disposition in (Bin.INCLUDE_TYPED,Bin.INCLUDE_UNTYPED):
    result.include = lname
  else:
    result.include = None

  result.tags   = []
  result.others = []
  result.ld     = []

  if largest.disposition == Bin.INCLUDE_UNTYPED:
    result.disposition = 'obligate-untyped'
  elif largest.disposition == Bin.INCLUDE_TYPED:
    result.disposition = 'obligate-typed'
  elif largest.disposition == Bin.EXCLUDE:
    result.disposition = 'obligate-exclude'
  else:
    result.disposition = 'maximal-bin'

  # Process each locus in the bin
  for lname,bin in bins.iteritems():
    # If the current bin is a superset of the reference set, then consider this locus
    # a tag.  The superset is needed to handle the case where the reference locus is
    # an obligate include and may not be the largest bin.
    if can_tag(bin, largest):
      result.tags.append(lname)

      # Update maximum coverage number for all candidate tags, except for
      # obligate include bins
      if largest.disposition not in (Bin.INCLUDE_TYPED,Bin.INCLUDE_UNTYPED):
        result.maxcovered = max(result.maxcovered,bin.maxcovered)
    else:
      result.others.append(lname)

  assert len(result.tags) >= result.tags_required

  for lname in result.tags:
    # Output the tags as self-pairs (r-squared=1,dprime=1)
    result.ld.append( (lname,lname,1.,1.) )

  # For each pair of loci in the bin, yield name, location, and LD info
  for lname1,lname2 in pair_generator(largest):
    if (lname1,lname2) not in lddata:
      lname1,lname2=lname2,lname1

    if (lname1,lname2) in lddata:
      r2,dprime = lddata.pop( (lname1,lname2) )
      result.ld.append( (lname1,lname2,r2,dprime) )

  return result


def binner(loci, binsets, lddata, includes, get_tags_required=None):
  '''
  Greedy tag marker binning algorithm -- similar to the Carlson algorithm.

  The binner utilizes the recomputed binsets and lddata which reflect LD
  values previously filtered against the choosen minimum threshold for bin.

  Given a set of binsets and a sequence of loci, the binner iteratively selects
  the largest bin in the following priority order:
    1) bin dispositions such that include > normal > exclude
    2) bins with the largest number of loci
    3) bins with the largest total MAF

  The binner constructs a BinResult object for each bin with the following
  attributes:
    1) tags: a list of all loci chosen as tags
    2) others: a list of all non-tag loci
    3) average_maf: the average of MAF of all loci in the bin
    4) include: the locus name of the obligate tag or None
    5) disposition: This attribute may take one of three possible values:
           'obligate-untyped' if the reference locus in include set and untyped
           'obligate-typed'   if the reference locus in include set and typed
           'obligate-exclude' if the reference locus in exclude set
           'maximal-bin' otherwise
    6) ld: a list of tuples of pairwise LD data within each bin.  Tags for
           that bin are encoded as records with the locus paired with itself
           and r-squared and dprime of 1:

               (BINNUM,LNAME,LNAME,1,1,DISPOSITION)

               DISPOSITION for tags takes one of the following values:

                 'untyped-tag'      if it is an obligate tag and has not been genotyped
                 'typed-tag'        if it is an obligate tag and is the reference tag of
                                    an obligate-typed bin
                 'redundant-tag'    an obligate tag and has been genotyped, but not the
                                    reference tag of a bin
                 'alternate-tag'    if it is a tag in an obligate-include
                                    bin, but not the obligate tag
                 'excluded-tag'     a tag for a bin that contains all
                                    obligatorily exluded loci
                 'candidate-tag'    a tag for a bin that has more than one
                                    possible non-obligate tag
                 'necessary-tag'    a tag for a bin that has only one tag,
                                    but covers at least one other locus
                 'lonely-tag'       a tag for a bin with no other loci, but
                                    originally covered more loci.  These
                                    additional loci where removed by
                                    previous iterations of the binning
                                    algorithm.  This disposition is
                                    primarily to distinguish these bins from
                                    singletons, which intrinsically are in
                                    insufficient Ld with any other locus.
                 'singleton-tag'    a tag that is not in significant LD with
                                    any other locus, based on the specified
                                    LD criteria

           The pairwise r-squared values within the bin follow in the form:
               (BINNUM,LNAME1,LNAME2,RSQUARED,DPRIME,DISPOSITION).

               DISPOSITION for these pairwise elements takes one of the
               following values:

                 'tag-tag'         for LD between tags within a bin;
                 'other-tag'       for LD between a non-tag and a tag
              &  'tag-other'         or a tag and a non-tag, respectively;
                 'other-other'     for LD between non-tags within a bin.

    7) maxcovered: the maximum number for loci that each candidate tag may
                   have covered.  The actual coverage may be smaller, due to
                   other loci removed by bins that were selected in an
                   earlier iteration of the algorithm.  For obligate include
                   bins, only the obligatory tags is considered, since
                   alternate tags are not considered.

  @type:  binsets: dictionary, key is of string type and value is a Bin object
  @param: binsets: dictionary that for each locus mapped to a Bin which includes
                   all loci satisfying rsquared threshold with this reference locus and its
                   own MAF greater than MAF threshold
  @type      loci: Sequence of (LNAME, LOCATION,MAF,...GENOTYPES...)
  @param     loci: A sequence of loci that may appear in the ldpairs

  @type    lddata: Sequence of (LNAME1,LNAME2,R-SQUARED,DPRIME)
  @param   lddata: A sequence of parwise LD information that exceed a given
                   threshold.  i.e, they must be pre-filtered by the r-squared
                   criteria before being passed to the binner.
  @rtype:          generator for an ordered sequence of BinResult objects
  @return:         the optimal bins with tags, others, ld informatin etc. for each
  '''

  bin_sequence = BinSequence(loci, binsets, lddata, get_tags_required)

  # Run while we still have loci to bin
  for lname,largest,bins in bin_sequence:
    yield build_result(lname, largest, bins, lddata, includes, get_tags_required)


def binner_vector(loci, binsets, lddata, includes, get_tags_required=None):
  bin_sequence = MultiBinSequence(loci, binsets, lddata, get_tags_required)

  for lname,largest,bins in bin_sequence:
    results = []
    for pop_largest,pop_bins,pop_lddata in izip(largest,bins,lddata):
      if pop_largest:
        result = build_result(lname, pop_largest, pop_bins, pop_lddata, includes, get_tags_required)
        results.append(result)
      else:
        results.append(None)

    yield lname,results


def generate_ldpairs_vector(args, include, subset, ldsubset, options):
  labels = get_populations(options.multipopulation)
  pops = len(labels)
  regions = len(args) // pops

  if len(args) % pops != 0:
    raise TagZillaError, 'ERROR: The number of input files must be a multiple of the number of populations'

  for i in xrange(regions):
    ldpairs = []
    multi_options = []
    locusmap = []

    for file_options,filename in args[i*pops:(i+1)*pops]:
      lmap = {}
      pairs = generate_ldpairs_from_file(filename, lmap, include, subset, ldsubset, file_options)

      ldpairs.append(pairs)
      locusmap.append(lmap)

    yield ldpairs,locusmap


def do_tagging_vector(ldpairs, includes, exclude, designscores, options):
  labels = get_populations(options.multipopulation)
  pops = len(labels)

  # If we require a total ordering, then build binsets from all ldpairs
  if options.targetbins or options.targetloci:
    sys.stderr.write('[%s] Building global binsets\n' % time.asctime())
    multi_ldpairs  = [ [] for p in xrange(pops) ]
    multi_locusmap = [ {} for p in xrange(pops) ]

    for region,lmap in ldpairs:
      for pop_ldpairs,pop_locusmap,pop_lmap,pairs in izip(multi_ldpairs,multi_locusmap,lmap,region):
        pop_ldpairs.append(pairs)
        update_locus_map(pop_locusmap,pop_lmap.itervalues())

    binsets = []
    lddata  = []
    for pop_ldpairs,pop_locusmap in izip(multi_ldpairs,multi_locusmap):
      pop_binsets,pop_lddata = build_binsets(pop_locusmap, pop_ldpairs, includes, exclude, designscores)
      binsets.append(pop_binsets)
      lddata.append(pop_lddata)

    sys.stderr.write('[%s] Choosing global bins\n' % time.asctime())
    bins = binner_vector(multi_locusmap, binsets, lddata, includes, get_tags_required_function(options))
    yield bins,lddata,multi_locusmap

  else:
    # Otherwise, process each sequence of ldpairs independently
    for pairs,locusmap in ldpairs:
      sys.stderr.write('[%s] Building binsets\n' % time.asctime())

      binsets = []
      lddata  = []
      for pop_ldpairs,pop_locusmap in izip(pairs,locusmap):
        pop_binsets,pop_lddata = build_binsets(pop_locusmap, [pop_ldpairs], includes, exclude, designscores)
        binsets.append(pop_binsets)
        lddata.append(pop_lddata)

      sys.stderr.write('[%s] Choosing bins\n' % time.asctime())
      bins = binner_vector(locusmap, binsets, lddata, includes, get_tags_required_function(options))
      yield bins,lddata,locusmap


def hapmap_geno(g):
  return intern(g.strip().replace('N',' '))


def linkage_geno(g):
  return intern(''.join(g).strip().replace('0',' '))


def prettybase_geno(g):
  return intern(''.join(g).strip().replace('N',' '))


def xtab(data, rowkeyfunc, colkeyfunc, valuefunc, aggregatefunc=None):
  '''
  Generalized cross-tab function to aggregate a spare representation
  (row,column,value) into a matrix of rows by columns with values optionally
  aggregated into a scalar using aggregatefunc.
  '''
  get0 = itemgetter(0)
  get1 = itemgetter(1)
  get2 = itemgetter(2)

  rowkeys  = {}
  colkeys  = {}
  datalist = []

  # Pass 1: Build row, column, and data list
  for row in data:
    rowkey = rowkeyfunc(row)
    colkey = colkeyfunc(row)
    value  = valuefunc(row)
    i=rowkeys.setdefault(rowkey, len(rowkeys))
    j=colkeys.setdefault(colkey, len(colkeys))
    datalist.append( (i,j,value) )

  # Invert and sort the row and column keys
  rowkeys = sorted(rowkeys.iteritems(), key=get1)
  colkeys = sorted(colkeys.iteritems(), key=get1)

  datalist.sort()

  # Output column metadata
  columns = map(get0, colkeys)
  rows    = map(get0, rowkeys)

  yield columns

  # Pass 2: Build and yield result rows
  for i,rowdata in groupby(datalist, get0):
    row = [None]*len(colkeys)

    for j,vs in groupby(rowdata, get1):
      row[j] = map(get2, vs)

    if aggregatefunc:
      for colkey,j in colkeys:
        row[j] = aggregatefunc(rows[i], colkey, row[j])

    yield rows[i],row


def genomerge(locus, sample, genos):
  '''
  Merge multiple genotypes for the same locus and sample
  '''
  if not genos:
    return '  '
  elif len(genos) == 1:
    return genos[0]

  genos = set(genos)
  genos.discard('  ')

  if not genos:
    return '  '
  elif len(genos) == 1:
    return genos.pop()
  else:
    return '  '


def load_prettybase_genotypes(filename, nonfounders):
  '''
  Loads the genome location, subject,and genotypes from a PrettyBase
  formatted file.  Returns a generator that yields successive Locus objects.
  '''

  gfile = autofile(filename, 'r', hyphen=sys.stdin)

  def _data():
    for line in gfile:
      fields = re_spaces.split(line.strip())

      if len(fields) < 4:
        continue

      locus,sample,a1,a2 = fields[:4]

      if nonfounders is not None and sample in nonfounders:
        continue

      yield locus,sample,prettybase_geno( (a1,a2) )

  loci = xtab(_data(),itemgetter(0),itemgetter(1),itemgetter(2),genomerge)
  samples = loci.next()

  for locus,genos in loci:
    try:
      yield Locus(locus, int(locus), genos)
    except ValueError:
      # Ignore invalid loci with just a warning, for now
      print >> sys.stderr, "WARNING: Invalid locus in file '%s', name '%s'" % (filename,fields[0])


def load_hapmap_genotypes(filename, nonfounders):
  '''
  Loads the RS#, Genome location, and genotypes from a HapMap formatted
  file.  Returns a generator that yields successive Locus objects.

  Genotypes strings are 'interned' to save massive amounts of memory.
  i.e. all 'AA' string objects refer to the same immutable string object,
  rather than each 'AA' genotype allocating a distinct string object.
  See the Python builtin 'intern' function for more details.
  '''

  gfile = autofile(filename, 'r', hyphen=sys.stdin)

  gfile = dropwhile(lambda s: s.startswith('#'), gfile)
  header = gfile.next()

  if not any(header.startswith(h) for h in HAPMAP_HEADERS):
    if filename == '-':
      filename = '<stdin>'
    raise TagZillaError, "ERROR: HapMap Input file '%s' does not seem to be a HapMap data file." % filename

  header = header.strip().split(' ')

  if nonfounders is None:
    indices = range(11,len(header))
  else:
    indices = [ i for i,name in enumerate(header) if i >= 11 and name not in nonfounders ]

  for line in gfile:
    fields = line.split(' ')
    n = len(fields)
    genos = [ hapmap_geno(fields[i]) for i in indices if i < n ]
    try:
      yield Locus(fields[0], int(fields[3]), genos)
    except (ValueError,KeyError):
      # Ignore invalid loci with just a warning, for now
      print >> sys.stderr, "WARNING: Invalid locus in file '%s', name '%s'" % (filename,fields[0])


def read_locus_file(filename):
  locfile = autofile(filename)
  locus_info = []

  try:
    for i,line in enumerate(locfile):
      fields = re_spaces.split(line.strip())

      if not fields:
        continue

      if len(fields) == 2:
        locus_info.append( (fields[0], int(fields[1])) )
      else:
        raise ValueError, 'Invalid locus file'

  except (ValueError,IndexError):
    raise TagZillaError, 'ERROR: Invalid line %d in locus file "%s".' % (i+1,filename)

  return locus_info


def linkage_genotypes(fields):
  '''Return an iterator that yields genotype from a Linkage record'''
  allele1s = islice(fields,6,None,2)
  allele2s = islice(fields,7,None,2)
  return map(linkage_geno, izip(allele1s,allele2s))


def load_linkage_genotypes(filename, loci):
  '''
  Load each individual genotypes from the linkage formatted file
  for all the founders and assign the loaded genotypes to each locus
  in loci and construct a list of Locus objects.

  Note that Genotypes strings are 'interned' to save massive amounts of memory.
  i.e. all 'AA' string objects refer to the same immutable string object,
  rather than each 'AA' genotype allocating a distinct string object.
  See the Python builtin 'intern' function for more details.

  @param filename: name of the linkage formatted genotype data file
  @type  filename: string
  @param     loci: the locus information
  @type      loci: set of tuples with each like (locusname, location)
  @rtype         : list
  @return        : list of Locus objects
  '''

  pedfile = autofile(filename)

  ind_genos = []
  for line in pedfile:
    fields = re_spaces.split(line.strip())

    # Filter out all non-founders
    if len(fields) < 10 or fields[2:4] != ['0','0']:
      continue

    ind_genos.append( linkage_genotypes(fields) )

  n = len(ind_genos)
  missing_geno = intern('  ')
  loci = [ Locus(lname, location, [missing_geno]*n) for lname,location in loci ]

  for i,genos in enumerate(ind_genos):
    for j,g in enumerate(genos):
      loci[j].genos[i] = g

  for locus in loci:
    locus.maf = estimate_maf(locus.genos)

  return loci


def load_raw_genotypes(filename, nonfounders=None):
  '''
  Loads the RS#, Genome location, and genotypes from a native formatted
  genotype file.  Returns a generator that yields successive Locus objects.

  Genotypes strings are 'interned' to save massive amounts of memory.
  i.e. all 'AA' string objects refer to the same immutable string object,
  rather than each 'AA' genotype allocating a distinct string object.
  '''

  gfile = autofile(filename, 'r', hyphen=sys.stdin)

  header = gfile.readline()

  if not header.startswith(GENO_HEADER):
    if filename == '-':
      filename = '<stdin>'
    raise TagZillaError, "ERROR: Genotype input file '%s' does not seem to be in the right format." % filename

  header = header.strip().split('\t')

  if nonfounders is None:
    indices = range(3,len(header))
  else:
    indices = [ i for i,name in enumerate(header) if i >= 3 and name not in nonfounders ]

  for line in gfile:
    fields = line.split('\t')
    n = len(fields)
    genos = [ intern(fields[i].strip() or '  ') for i in indices if i < n ]
    yield Locus(fields[0], int(fields[2]), genos)


def load_ldat_genotypes(filename, loci, nonfounders=None):
  '''
  Loads the RS# and genotypes from a ldat genotype file.  Returns a
  generator that yields successive Locus objects.

  Genotypes strings are 'interned' to save massive amounts of memory.
  i.e. all 'AA' string objects refer to the same immutable string object,
  rather than each 'AA' genotype allocating a distinct string object.
  '''
  loci = dict(loci)

  gfile = autofile(filename, 'r', hyphen=sys.stdin)

  header = gfile.readline()

  header = header.strip().split('\t')

  if nonfounders is None:
    indices = range(1,len(header))
  else:
    indices = [ i for i,name in enumerate(header) if i >= 1 and name not in nonfounders ]

  for line in gfile:
    fields = line.split('\t')
    n = len(fields)
    genos = [ intern(fields[i].strip() or '  ') for i in indices if i < n ]
    yield Locus(fields[0], loci[fields[0]], genos)


def load_festa_file(filename, locusmap, subset, rthreshold):
  '''
  Load FESTA formatted file that contain pre-computed LD data for pairs of loci
  '''
  ldfile = autofile(filename)
  header = ldfile.readline()

  for line in ldfile:
    lname1,lname2,ldvalue = re_spaces.split(line.strip())
    ldvalue = float(ldvalue)

    if subset and (lname1 not in subset or lname2 not in subset):
      continue

    if lname1 not in locusmap:
      locusmap[lname1] = Locus(lname1, 0, [])

    if lname2 not in locusmap:
      locusmap[lname2] = Locus(lname2, 0, [])

    if ldvalue >= rthreshold:
      yield lname1,lname2,ldvalue,0


def load_hapmapld_file(filename, locusmap, subset, maxd, rthreshold, dthreshold):
  '''
  Load Hapmap formatted file that contain pre-computed LD data for pairs of loci
  '''
  ldfile = autofile(filename)
  ldfile = dropwhile(lambda s: s.startswith('#'), ldfile)

  for line in ldfile:
    loc1,loc2,pop,lname1,lname2,dprime,r2,lod = line.strip().split(' ')

    if subset and (lname1 not in subset or lname2 not in subset):
      continue

    loc1 = int(loc1)
    loc2 = int(loc2)

    if lname1 not in locusmap:
      locusmap[lname1] = Locus(lname1, loc1, [])

    if lname2 not in locusmap:
      locusmap[lname2] = Locus(lname2, loc2, [])

    if abs(loc1-loc2) > maxd:
      continue

    dprime = float(dprime)
    r2     = float(r2)

    if r2 >= rthreshold and abs(dprime) >= dthreshold:
      yield lname1,lname2,r2,dprime


def read_hapmap_nonfounders(filename, nonfounders):
  pedfile = autofile(filename)

  founder = ['0','0']
  for line in pedfile:
    fields = line.strip().split('\t')

    # Filter out all founders
    if fields[2:4] == founder or len(fields) < 7:
      continue

    sample = fields[6].split(':')[-2]
    nonfounders.add(sample)

  return nonfounders


def read_linkage_nonfounders(filename, nonfounders):
  pedfile = autofile(filename)

  founder = ['0','0']
  for line in pedfile:
    fields = re_spaces.split(line.strip())

    # Filter out all non-founders
    if fields[2:4] == founder:
      continue

    nonfounders.add(tuple(fields[0:2]))

  return nonfounders


def read_snp_list(name, sset):
  if name.startswith(':'):
    sset.update(name[1:].split(','))
    return

  sfile = autofile(name)
  for line in sfile:
    fields = re_spaces.split(line.strip())
    if fields:
      sset.add(fields[0])

  return sset


def build_design_score(designscores):
  designscores = designscores or []
  aggscores = {}
  for design in designscores:
    design = design.split(':')
    dfile = design[0]
    threshold = 0.0
    scale = 1.0
    if len(design) > 1:
      threshold = float(design[1])
    elif len(design) > 2:
      scale = float(design[2])
    scores = read_design_score(dfile)
    for lname,score in scores:
      if score < threshold:
        score = 0.0
      aggscores[lname] = aggscores.get(lname,1.0)*score*scale
  return aggscores


def build_tag_criteria(tagcriteria):
  weights = {}
  tagcriteria = tagcriteria or []
  for c in tagcriteria:
    c = c.lower().split(':')
    method = c[0]
    weight = 2 #default weight
    if len(c) > 1:
      weight = float(c[1])
    weights[method] = weight
  return weights


class TagSelector(object):
  default_weight = 2.0

  def __init__(self, scores, weights):
    self.scores  = scores
    self.weights = weights

  def select_tags(self,bin):
    if not self.weights and not self.scores:
      return

    if not self.weights and bin.disposition == 'obligate-exclude':
      return

    if len(bin.tags) == 1:
      bin.recommended_tags = list(bin.tags)[:1]
      return

    weights = {}
    for method,weight in self.weights.iteritems():
      w = self.build_weights(bin, method, weight)
      for lname, weight in w.iteritems():
        weights[lname] = weights.get(lname,1) * weight

    if bin.disposition == 'obligate-exclude':
      allscores = {}
    else:
      allscores = self.scores

    # Default score: 0 if scores exist, 1 otherwise
    default_score = not allscores

    scores = []
    for tag in bin.tags:
      score  = allscores.get(tag,default_score)
      weight = weights.get(tag,1)
      s = score * weight
      scores.append( (s,tag) )

    scores.sort(reverse=True)

    # Store tags in weight order
    bin.tags = [lname for s, lname in scores]

    # Recommend tags
    bin.recommended_tags = bin.tags[:bin.tags_required]
    if bin.include is not None and bin.include not in bin.recommended_tags:
      bin.recommended_tags = [bin.include]+bin.recommended_tags[:bin.tags_required-1]

  def build_weights(self, bin, method, weight):
    if not method:
      return {}

    # Lexically-scoped weighting functions
    def maxsnp():
      w[lname1] = min(w.get(lname1,1),r2)
    def avgsnp():
      w[lname1] = w.get(lname1,0) + r2
    def maxtag():
      if lname2 not in bin.tags:
        maxsnp()
    def avgtag():
      if lname2 not in bin.tags:
        avgsnp()

    func = locals().get(method.lower(),None)

    if not callable(func):
      raise InternalError,'Invalid tag information criterion specified'

    w = {}
    for lname1,lname2,r2,dprime in bin.ld:
      if lname1==lname2:
        continue
      for lname1,lname2 in [(lname1,lname2),(lname2,lname1)]:
        if lname1 in bin.tags:
          func()

    if not w:
      return {}

    maxval = max(w.itervalues())

    weights = {}
    for tag in bin.tags:
      if abs(w[tag] - maxval) > 1e-10:
        weights[tag] = 1./weight

    return weights


def read_design_score(filename):
  sf = autofile(filename)
  for line in sf:
    fields = re_spaces.split(line.strip())
    lname = fields[0]
    try:
      score = float(fields[1])
      yield lname,score

    except ValueError:
      pass


def read_illumina_design_score(filename):
  sf = autofile(filename)
  header = sf.next.split(',')
  design_index = header.index('SNP_Score')
  for line in sf:
    fields = line.split(',')
    lname = fields[0]
    try:
      score = float(fields[design_index])
      yield lname,score

    except ValueError:
      pass


def load_genotypes(filename, options):
  options.format = options.format.lower()
  if options.format not in ('','hapmap','raw','ldat','linkage','hapmapld','festa','prettybase'):
    raise TagZillaError, 'ERROR: Unknown genotype/ld data format specified: "%s"' % options.format

  if options.format == '':
    if options.loci:
      options.format = 'linkage'
    else:
      options.format = 'hapmap'

  nonfounders = None
  if options.pedfile:
    if not options.pedformat:
      if options.format == 'hapmap':
        options.pedformat = 'hapmap'
      else:
        options.pedformat = 'linkage'

    nonfounders = set()
    if options.pedformat.lower() == 'hapmap':
      for pedfile in options.pedfile:
        read_hapmap_nonfounders(pedfile, nonfounders)
    elif options.pedformat.lower() == 'linkage':
      for pedfile in options.pedfile:
        read_linkage_nonfounders(pedfile, nonfounders)
    else:
      raise TagZillaError, 'ERROR: Unsupported pedigree file format specified: %s' % options.format

  locus_info = None
  if options.loci:
    if options.format not in ('linkage','ldat'):
      # XXX: This warning will trigger spuriously for multi-format input parameters
      #print >> sys.stderr, 'WARNING: It is not meaningful to specify a locus info file when not reading data in Linkage format.'
      pass

    locus_info = read_locus_file(options.loci)

  if options.format == 'hapmap':
    loci = load_hapmap_genotypes(filename, nonfounders)
  elif options.format == 'raw':
    loci = load_raw_genotypes(filename, nonfounders)
  elif options.format == 'prettybase':
    loci = load_prettybase_genotypes(filename, nonfounders)
  elif options.format == 'ldat':
    if not locus_info:
      raise TagZillaError, 'ERROR: Cannot load ldat format data since no loci are defined.'
    loci = load_ldat_genotypes(filename, locus_info, nonfounders)
  elif options.format == 'linkage':
    if not locus_info:
      raise TagZillaError, 'ERROR: Cannot load Linkage format data since no loci are defined.'
    loci = load_linkage_genotypes(filename, locus_info)
  else:
    raise TagZillaError, 'ERROR: Cannot load genotype data in %s format' % options.format

  if options.limit:
    loci = islice(loci,options.limit)

  return loci


def filter_loci(loci, include, subset, options):
  if options.obmaf is None:
    options.obmaf = options.maf

  if options.maf or options.obmaf:
    loci = filter_loci_by_maf(loci, options.maf, options.obmaf, include)

  if options.subset:
    loci = filter_loci_by_inclusion(loci, subset)

  if options.range:
    loci = filter_loci_by_range(loci, options.range)

  if options.mincompletion or options.mincompletionrate:
    loci = filter_loci_by_completion(loci, options.mincompletion, options.mincompletionrate/100.)

  if options.hwp:
    loci = filter_loci_by_hwp(loci, options.hwp)

  return loci


def materialize_loci(loci):
  loci = list(loci)

  def locus_key(l):
    return l.location,l.name

  loci.sort(key=locus_key)

  return loci


def scan_loci_ldsubset(monitor, loci, maxd):
  '''
  Yield only loci where there exists at least one monitored location within
  maxd distance.

  This implementation performs two linear passes over the list of loci, one
  forward and one in reverse.  As such, a locus may be yielded twice if it
  is within maxd if a monitored location on both the left and the right.
  '''
  n = len(loci)
  monitor = sorted(monitor)

  # Scan forward though loci, yielding all following loci within maxd of a
  # monitored location
  pos = 0
  for m in monitor:
    while pos < n and loci[pos].location < m:
      pos += 1
    while pos < n and loci[pos].location-m <= maxd:
      yield loci[pos]
      pos += 1

  # Scan backward though loci, yielding all prior loci within maxd if a
  # monitored location
  pos = n-1
  for m in reversed(monitor):
    while pos >= 0 and loci[pos].location > m:
      pos -= 1
    while pos >= 0 and m-loci[pos].location <= maxd:
      yield loci[pos]
      pos -= 1


def filter_loci_ldsubset(loci, ldsubset, maxd):
  if not ldsubset:
    return loci

  locusmap = dict( (l.name,l.location) for l in loci )
  monitor  = (locusmap[l] for l in ldsubset if l in locusmap)
  keep     = set(scan_loci_ldsubset(monitor,loci,maxd))
  return [ l for l in loci if l in keep ]


def update_locus_map(locusmap, loci):
  addloci = dict( (locus.name,locus) for locus in loci )
  overlap = set(addloci).intersection(locusmap)
  if overlap:
    raise TagZillaError, 'ERROR: Genotype files may not contain overlapping loci'
  locusmap.update(addloci)


def generate_ldpairs_single(args, locusmap, include, subset, ldsubset, options):
  for file_options,filename in args:
    yield generate_ldpairs_from_file(filename, locusmap, include, subset, ldsubset, file_options)


def generate_ldpairs_from_file(filename, locusmap, include, subset, ldsubset, options):
  sys.stderr.write('[%s] Processing input file %s\n' % (time.asctime(),filename))
  format = options.format.lower()

  if format == 'festa':
    return load_festa_file(filename, locusmap, subset, options.r)

  elif format == 'hapmapld':
    return load_hapmapld_file(filename, locusmap, subset, options.maxdist*1000, options.r, options.d)

  else: # generate from genotype file
    loci = load_genotypes(filename, options)
    loci = filter_loci(loci, include, subset, options)
    loci = materialize_loci(loci)
    loci = filter_loci_ldsubset(loci, ldsubset, options.maxdist*1000)

    # Locusmap must contain only post-filtered loci
    update_locus_map(locusmap, loci)
    return scan_ldpairs(loci, options.maxdist*1000, options.r, options.d)


def get_populations(option):
  if not option:
    return ['']

  try:
    n = int(option)
    labels = [ str(i) for i in xrange(1,n+1) ]

  except ValueError:
    labels = [ l.strip() for l in option.split(',') if l.strip() ]

  return labels


def generate_ldpairs_multi(args, locusmap, include, subset, ldsubset, options):
  # FIXME: This can be easily corrected
  formats = set(file_options.format.lower() for file_options,filename in args)
  if formats.intersection( ('festa','hapmapld') ):
    raise TagZillaError, 'ERROR: Multipopulation binning algoritm cannot currently accept data in LD/FESTA format.'

  method = options.multimethod.lower()
  labels = get_populations(options.multipopulation)
  pops = len(labels)
  regions = len(args) // pops

  if len(args) % pops != 0:
    raise TagZillaError, 'ERROR: The number of input files must be a multiple of the number of populations'

  for i in xrange(regions):
    multi_loci = []
    multi_options = []

    for file_options,filename in args[i*pops:(i+1)*pops]:
      loci = list(load_genotypes(filename, file_options))

      if method not in ('merge2','merge2+'):
        loci = filter_loci(loci, include, subset, file_options)
        loci = materialize_loci(loci)
        # Locusmap must contain only post-filtered loci
        locusmap.update( (locus.name,locus) for locus in loci if locus.genos )
      else:
        loci = materialize_loci(loci)

      multi_loci.append(loci)
      multi_options.append(file_options)

    if method in ['minld']:
      ldpairs = scan_ldpairs_multi(multi_loci, options.maxdist*1000, multi_options)

    elif method in ['merge2','merge3']:
      loci = merge_loci(multi_loci)

      if method in ['merge2']:
        loci = filter_loci(loci, include, subset, options)

      # Locusmap must contain only post-filtered loci
      loci = list(loci)
      locusmap.update( (locus.name,locus) for locus in loci if locus.genos )

      ldpairs = scan_ldpairs(loci, options.maxdist*1000, options.r, options.d)

    else:
      raise TagZillaError, 'ERROR: Unsupported multipopulation method (--multimethod) chosen: %s' % options.multimethod

    yield ldpairs


def generate_ldpairs(args, locusmap, include, subset, ldsubset, options):
  pops = len(get_populations(options.multipopulation))
  if pops > 1:
    method = (options.multimethod or '').lower()

    if pops <= 1 or not method:
      raise TagZillaError, 'ERROR: Multipopulation analysis requires specification of both -M/--multipopulation and --multimethod'

    if method not in MULTI_METHODS_S:
      raise TagZillaError, 'ERROR: Unsupported multipopulation method (--multimethod) chosen: %s' % options.multimethod

    ldpairs = generate_ldpairs_multi(args, locusmap, include, subset, ldsubset, options)

  else:
    ldpairs = generate_ldpairs_single(args, locusmap, include, subset, ldsubset, options)

  return ldpairs


def get_tags_required_function(options):
  if options.locipertag:
    return lambda n: min(int(n//options.locipertag)+1,n)
  elif options.loglocipertag:
    l = log(options.loglocipertag)
    return lambda n: int(ceil(log(n+1)/l))
  else:
    return None


class TagZillaOptionParser(optparse.OptionParser):
  def _process_args(self, largs, rargs, values):
    '''_process_args(largs : [string],
                     rargs : [string],
                     values : Values)

    Process command-line arguments and populate 'values', consuming
    options and arguments from 'rargs'.  If 'allow_interspersed_args' is
    false, stop at the first non-option argument.  If true, accumulate any
    interspersed non-option arguments in 'largs'.
    '''
    while rargs:
      arg = rargs[0]
      # We handle bare '--' explicitly, and bare '-' is handled by the
      # standard arg handler since the short arg case ensures that the
      # len of the opt string is greater than 1.
      if arg == '--':
        del rargs[0]
        return
      elif arg.startswith('--'):
        # process a single long option (possibly with value(s))
        self._process_long_opt(rargs, values)
      elif arg.startswith('-') and len(arg) > 1:
        # process a cluster of short options (possibly with
        # value(s) for the last one only)
        self._process_short_opts(rargs, values)
      elif self.allow_interspersed_args:
        self._process_arg(arg)
      else:
        return

  def _process_arg(self, arg):
    self.largs.append( (copy.deepcopy(self.values),arg) )
    del self.rargs[0]


def option_parser():
  usage = 'usage: %prog [options] genofile... [options] genofile...'
  parser = TagZillaOptionParser(usage=usage, add_help_option=False)

  parser.add_option('-h', '--help', dest='help', action='store_true',
                        help='show this help message and exit')
  parser.add_option('--license', dest='license', action='store_true',
                          help="show program's copyright and license terms and exit")
  parser.add_option('--profile', dest='profile', metavar='P', help=optparse.SUPPRESS_HELP)

  inputgroup = optparse.OptionGroup(parser, 'Input options')

  inputgroup.add_option('-f', '--format', dest='format', metavar='NAME', default='',
                          help='Format for genotype/pedigree or ld input data.  Values: hapmap (default), linkage, prettybase, ldat, raw, festa, hapmapld.')
  inputgroup.add_option(      '--pedformat', dest='pedformat', metavar='NAME', default='',
                          help='Format for pedigree data.  Values: hapmap or linkage.  Defaults to hapmap when '
                               'reading HapMap files and linkage format otherwise.')
  inputgroup.add_option('-l', '--loci', dest='loci', metavar='FILE',
                          help='Locus description file for input in Linkage format')
  inputgroup.add_option('-p', '--pedfile', dest='pedfile', metavar='FILE', action='append',
                          help='Pedigree file for HapMap or PrettyBase data files (optional)')
  inputgroup.add_option('-e', '--excludetag', dest='exclude', metavar='FILE', default='',
                          help='File containing loci that are excluded from being a tag')
  inputgroup.add_option('-i', '--includeuntyped', dest='include_untyped', metavar='FILE', default='',
                          help='File containing loci that are obligatorily tags and untyped (may not cover another obligate locus)')
  inputgroup.add_option('-I', '--includetyped', dest='include_typed', metavar='FILE', default='',
                          help='File containing loci that are obligatorily tags but have been typed (may cover another typed locus)')

  inputgroup.add_option('-s', '--subset', dest='subset', metavar='FILE', default='',
                          help='File containing loci to be used in analysis')
  inputgroup.add_option('-S', '--ldsubset', dest='ldsubset', metavar='FILE', default='',
                          help='File containing loci within the region these loci LD will be analyzed (see -d/--maxdist)')
  inputgroup.add_option('-R', '--range', dest='range', metavar='S-E,...', default='',
                          help='Ranges of genomic locations to analyze, specified as a comma seperated list of start and '
                               'end coordinates "S-E".  If either S or E is not specified, then the ranges are assumed '
                               'to be open.  The end coordinate is exclusive and not included in the range.')
  inputgroup.add_option('-D', '--designscores', dest='designscores', metavar='FILE', type='str', action='append',
                          help='Read in design scores or other weights to use as criteria to choose the optimal tag for each bin')
  inputgroup.add_option('-L', '--limit', dest='limit', metavar='N', type='int', default=0,
                          help='Limit the number of loci considered to N for testing purposes (default=0 for unlimited)')

  outputgroup = optparse.OptionGroup(parser, 'Output options')

  outputgroup.add_option('-b', '--summary', dest='sumfile', metavar='FILE', default='-',
                          help="Output summary tables FILE (default='-' for standard out)")
  outputgroup.add_option('-B', '--bininfo', dest='bininfo', metavar='FILE',
                          help='Output summary information about each bin to FILE')
  outputgroup.add_option('-H', '--histomax', dest='histomax', metavar='N', type='int', default=10,
                          help='Largest bin size output in summary histogram output (default=10)')
  outputgroup.add_option('-k', '--skip', dest='skip', default=0, action='count',
                          help='Skip output of untagged or excluded loci')
  outputgroup.add_option('-o', '--output', dest='outfile', metavar='FILE', default=None,
                          help="Output tabular LD information for bins to FILE ('-' for standard out)")
  outputgroup.add_option('-O', '--locusinfo', dest='locusinfo', metavar='FILE',
                          help='Output locus information to FILE')
  outputgroup.add_option('-u', '--saveldpairs', dest='saveldpairs', metavar='FILE',
                          help='Output pairwise LD estimates to FILE')
  outputgroup.add_option('-x', '--extra', dest='extra', action='count',
                          help='Output inter-bin LD statistics')

  genoldgroup = optparse.OptionGroup(parser, 'Genotype and LD estimation options')

  genoldgroup.add_option('-a', '--minmaf', dest='maf', metavar='FREQ', type='float', default=0.05,
                          help='Minimum minor allele frequency (MAF) (default=0.05)')
  genoldgroup.add_option('-A', '--minobmaf', dest='obmaf', metavar='FREQ', type='float', default=None,
                          help='Minimum minor allele frequency (MAF) for obligate tags (defaults to -a/--minmaf)')
  genoldgroup.add_option('-c', '--mincompletion', dest='mincompletion', metavar='N', default=0, type='int',
                          help='Drop loci with less than N valid genotypes (default=0)')
  genoldgroup.add_option(      '--mincompletionrate', dest='mincompletionrate', metavar='N%', default=0, type='float',
                          help='Drop loci with completion rate less than N% (0-100) (default=0)')
  genoldgroup.add_option('-m', '--maxdist', dest='maxdist', metavar='D', type='int', default=200,
                          help='Maximum inter-marker distance in kb for LD comparison (default=200)')
  genoldgroup.add_option('-P', '--hwp', dest='hwp', metavar='p', default=None, type='float',
                          help='Filter out loci that fail to meet a minimum signficance level (pvalue) for a '
                               'test Hardy-Weignberg proportion (no default)')

  bingroup = optparse.OptionGroup(parser, 'Binning options')

  bingroup.add_option('-d', '--dthreshold', dest='d', metavar='DPRIME', type='float', default=0.,
                          help='Minimum d-prime threshold to output (default=0)')
  bingroup.add_option('-M', '--multipopulation', dest='multipopulation', metavar='N or P1,P2,...',
                          help='Multipopulation tagging where every N input files represent a group of populations. '
                               'May be specified as an integer N or a comma separated list of population labels.')
  bingroup.add_option(      '--multimethod', dest='multimethod', type='str', metavar='METH', default='global',
                          help='Merge populations when performing multipopulation tagging.  '
                               'METH may be %s. (default=global)' % ', '.join(MULTI_METHODS))
  bingroup.add_option('-r', '--rthreshold', dest='r', metavar='N', type='float', default=0.8,
                          help='Minimum r-squared threshold to output (default=0.8)')
  bingroup.add_option('-t', '--targetbins', dest='targetbins', metavar='N', type='int', default=0,
                          help='Stop when N bins have been selected (default=0 for unlimited)')
  bingroup.add_option('-T', '--targetloci', dest='targetloci', metavar='N', type='int', default=0,
                          help='Stop when N loci have been tagged (default=0 for unlimited)')
  bingroup.add_option('-C', '--tagcriteria', dest='tagcriteria', type='str', metavar='crit', action='append',
                          help='Use the specified criteria to choose the optimal tag for each bin')
  bingroup.add_option('-z', '--locipertag', dest='locipertag', metavar='N', type='int', default=None,
                          help='Ensure that bins contain more than one tag per N loci.  Bins with an insufficient number of tags will be reduced.')
  bingroup.add_option('-Z', '--loglocipertag', dest='loglocipertag', metavar='B', type='float', default=None,
                          help='Ensure that bins contains more than one tag per log_B(loci).  Bins with an insufficient number of tags will be reduced.')
  bingroup.add_option('--skipbinning', dest='skipbinning', action='count',
                          help='Skip binning step.  Typically used in conjunction with -u/--saveldpairs')

  parser.add_option_group(inputgroup)
  parser.add_option_group(outputgroup)
  parser.add_option_group(genoldgroup)
  parser.add_option_group(bingroup)

  return parser


def do_tagging(ldpairs, locusmap, includes, exclude, designscores, options):
  # If we require a total ordering, then build binsets from all ldpairs
  if options.targetbins or options.targetloci:
    sys.stderr.write('[%s] Building global binsets\n' % time.asctime())
    binsets,lddata = build_binsets(locusmap, ldpairs, includes, exclude, designscores)
    sys.stderr.write('[%s] Choosing global bins\n' % time.asctime())
    bins = binner(locusmap, binsets, lddata, includes, get_tags_required_function(options))
    yield bins,lddata
  else:
    # Otherwise, process each sequence of ldpairs independently
    for pairs in ldpairs:
      sys.stderr.write('[%s] Building binsets\n' % time.asctime())
      binsets,lddata = build_binsets(locusmap, [pairs], includes, exclude, designscores)
      sys.stderr.write('[%s] Choosing bins\n' % time.asctime())
      bins = binner(locusmap, binsets, lddata, includes, get_tags_required_function(options))
      yield bins,lddata


def build_output(options, exclude):
  pairinfofile = None
  if options.outfile:
    pairinfofile = autofile(options.outfile, 'w', hyphen=sys.stdout)
    pairinfo = PairwiseBinOutput(pairinfofile, exclude)
  else:
    pairinfo = NullPairwiseBinOutput()

  locusinfofile = None
  if options.locusinfo:
    locusinfofile = autofile(options.locusinfo, 'w', hyphen=sys.stdout)
    locusinfo = LocusOutput(locusinfofile, exclude)
  else:
    locusinfo = NullLocusOutput()

  infofile = None
  if options.bininfo:
    infofile = autofile(options.bininfo, 'w', hyphen=sys.stdout)

  if options.bininfo or options.sumfile:
    bininfo = BinInfo(infofile, options.histomax+1)
  else:
    bininfo = NullBinInfo()

  sumfile = autofile(options.sumfile, 'w', hyphen=sys.stdout)

  if [pairinfofile,locusinfofile,infofile,sumfile].count(sys.stdout) > 1:
    raise TagZillaError, 'ERROR: More than one output file directed to standard out.'

  return pairinfo,locusinfo,bininfo,sumfile


class Includes(object):
  def __init__(self, typed, untyped):
    self.typed   = typed - untyped
    self.untyped = untyped

  def __contains__(self, other):
    return other in self.typed or other in self.untyped

  def __iter__(self):
    return chain(self.typed,self.untyped)

  def __len__(self):
    return len(self.typed)+len(self.untyped)


def tagzilla(options,args):
  pops = len(get_populations(options.multipopulation))

  if pops > 1:
    method = (options.multimethod or '').lower()

    if pops <= 1 or not method:
      raise TagZillaError, 'ERROR: Multipopulation analysis requires specification of both -M/--multipopulation and --multimethod'

    if method not in MULTI_METHODS:
      raise TagZillaError, 'ERROR: Unsupported multipopulation method (--multimethod) chosen: %s' % options.multimethod

    if method in MULTI_METHODS_M:
      return tagzilla_multi(options,args)

  return tagzilla_single(options,args)


def tagzilla_single(options,args):

  subset          = set()
  include_untyped = set()
  include_typed   = set()
  exclude         = set()
  ldsubset        = set()

  if options.subset:
    read_snp_list(options.subset, subset)

  if options.ldsubset:
    read_snp_list(options.ldsubset, ldsubset)

  if options.include_untyped:
    read_snp_list(options.include_untyped, include_untyped)

  if options.include_typed:
    read_snp_list(options.include_typed, include_typed)

  if options.exclude:
    read_snp_list(options.exclude, exclude)

  includes     = Includes(include_typed, include_untyped)
  designscores = build_design_score(options.designscores)
  tagcriteria  = build_tag_criteria(options.tagcriteria)
  tagselector  = TagSelector(designscores, tagcriteria)

  locusmap = {}
  ldpairs = generate_ldpairs(args, locusmap, includes, subset, ldsubset, options)

  if options.saveldpairs:
    ldpairs = save_ldpairs(options.saveldpairs, ldpairs)

  if options.skipbinning:
    # Fast trick to save all ld results, but not store them
    for pairs in ldpairs:
      list(dropwhile(lambda x: True, pairs))
    return

  results = do_tagging(ldpairs, locusmap, includes, exclude, designscores, options)

  pairinfo,locusinfo,bininfo,sumfile = build_output(options, exclude)

  binned_loci = 0
  binnum = 0
  tags = set()

  try:
    population = get_populations(options.multipopulation)[0]
  except IndexError:
    pass

  population = population or 'user specified'

  for bins,lddata in results:
    for bin in bins:
      binnum += 1
      bin.binnum = binnum

      qualifier = bin_qualifier(bin, binned_loci, options)

      # Update binned loci after determining qualifier
      binned_loci += len(bin)

      tags.update(bin.tags)

      tagselector.select_tags(bin)

      bininfo.emit_bin(bin, locusmap, exclude, population)
      pairinfo.emit_bin(bin, qualifier, population, options)
      locusinfo.emit_bin(bin, locusmap, qualifier, population)

    # Process remaining items in lddata and output residual ld information
    # (i.e. the inter-bin pairwise)
    if options.extra:
      pairinfo.emit_extra(lddata, tags, population)

    # Clear locus data after all bins are processed to save memory
    locusmap.clear()

  # Emit useful bin summary table
  bininfo.emit_summary(sumfile, population)


def tag_intersection(results):
  ires = (r for r in results if r is not None)
  tags = set()

  tags.update(ires.next())

  for r in ires:
    tags.intersection_update(r.tags)

  return tags


def subset_tags(result, tags, recommended=None):
  diff = set(result.tags) - tags
  result.tags = tags
  result.others.extend(diff)
  if recommended:
    result.recommended_tags = list(recommended.intersection(tags))


def tagzilla_multi(options,args):

  subset          = set()
  ldsubset        = set()
  include_untyped = set()
  include_typed   = set()
  exclude         = set()

  if options.subset:
    read_snp_list(options.subset, subset)

  if options.ldsubset:
    read_snp_list(options.ldsubset, ldsubset)

  if options.include_untyped:
    read_snp_list(options.include_untyped, include_untyped)

  if options.include_typed:
    read_snp_list(options.include_typed, include_typed)

  if options.exclude:
    read_snp_list(options.exclude, exclude)

  includes     = Includes(include_typed, include_untyped)
  designscores = build_design_score(options.designscores)
  tagcriteria  = build_tag_criteria(options.tagcriteria)
  tagselector  = TagSelector(designscores, tagcriteria)

  pairinfo,locusinfo,bininfo,sumfile = build_output(options, exclude)

  ldpairs = generate_ldpairs_vector(args, includes, subset, ldsubset, options)
  results = do_tagging_vector(ldpairs, includes, exclude, designscores, options)

  labels = get_populations(options.multipopulation)

  binnum = 0
  binned_loci = {}
  poptags = {}
  popdtags = {}

  for resultset,lddata,locusmap in results:
    try:
      tags,resultset = zip(*resultset)
    except ValueError:
      tags,resultset = [],[]

    # Refine tags -- once enabled, also add stags to subset_tags below
    if 0:
      results = zip(*resultset)
      mtags   = set(merge_bins(results))
      stags   = set(shrink_tags(mtags, results))
      print len(tags),len(mtags),len(stags)

    for res in resultset:
      tags = tag_intersection(res)
      # FIXME: This should eventually set the intersection as the bin.tags
      #        and exclude all other candidate tags
      recommended = [iter(tags).next()]

      binnum += 1
      disposition = None
      for population,bin,lmap in izip(labels,res,locusmap):
        if bin is not None:
          bin.binnum = binnum

          # FIXME: This should eventually set the intersection as the bin.tags
          #        and exclude all other candidate tags.
          subset_tags(bin, tags)

          qualifier = bin_qualifier(bin, binned_loci.get(population,0), options)

          # Update binned loci after determining qualifier
          binned_loci[population] = binned_loci.get(population,0) + len(bin)

          poptags.setdefault(population, set()).update(bin.tags)
          disposition = bin.disposition

          # FIXME: The recommeneded tag must be selected for this method to
          #        ensure across-population coverage.
          #        tagselector.select_tags(bin) must be extended to pick the
          #        recommended among several parallel bins.
          bin.recommended_tags = recommended

          bininfo.emit_bin(bin, lmap, exclude, population)
          pairinfo.emit_bin(bin, qualifier, population, options)
          locusinfo.emit_bin(bin, lmap, qualifier, population)

      popdtags[disposition] = popdtags.get(disposition,0) + 1

    # Process remaining items in lddata and output residual ld information
    # (i.e. the inter-bin pairwise)
    if options.extra:
      for ldd,population in izip(lddata,labels):
        pairinfo.emit_extra(ldd, poptags[population], population)

  # Emit useful bin summary table
  for population in labels:
    bininfo.emit_summary(sumfile, population)

  bininfo.emit_multipop_summary(sumfile, popdtags)


def run_profile(progmain,options,args):
  if not getattr(options,'profile',None):
    return progmain(options,args)

  if options.profile == 'python':
    try:
      import cProfile as profile
    except ImportError:
      import profile
    import pstats

    prof = profile.Profile()
    try:
      return prof.runcall(progmain, options, args)
    finally:
      stats = pstats.Stats(prof)
      stats.strip_dirs()
      stats.sort_stats('time', 'calls')
      stats.print_stats(25)

  elif options.profile == 'hotshot':
    import hotshot, hotshot.stats
    prof = hotshot.Profile('tmp.prof')
    try:
      return prof.runcall(progmain, options, args)
    finally:
      prof.close()
      stats = hotshot.stats.load('tmp.prof')
      stats.strip_dirs()
      stats.sort_stats('time', 'calls')
      stats.print_stats(25)

  else:
    raise TagZillaError, 'ERROR: Unknown profiling option provided "%s"' % options.profile


def format_elapsed_time(t):
  units = [('s',60),('m',60),('h',24),('d',365),('y',None)]

  elapsed = []
  for symbol,divisor in units:
    if divisor:
      t,e = divmod(t,divisor)
    else:
      t,e = 0,t

    if e:
      if symbol == 's':
        elapsed.append('%.2f%s' % (e,symbol))
      else:
        elapsed.append('%d%s' % (e,symbol))

    if not t:
      break

  elapsed.reverse()
  return ''.join(elapsed) or '0s'


def check_accelerators(accelerators, quiet=False):
    failed = []
    for a in accelerators:
      try:
        __import__(a)
      except ImportError:
        failed.append(a)

    if failed and not quiet:
      print 'WARNING: Failed to import the following native code accelerators:'
      print '            ',', '.join(failed)
      print '''\
         This will result in significantly increased computation times.
         Please do not post comparitive timing or benchmarking data when
         running in this mode.
'''
    return not failed


def launcher(progmain, opt_parser,
                       __program__      = '',
                       __version__      = '0.0',
                       __authors__      = [],
                       __copyright__    = '',
                       __license__      = '',
                       __accelerators__ = [],
                       **kwargs):

  if __program__ and __version__:
    print '%s version %s\n' % (__program__,__version__)
  elif __program__:
    print __program__
  elif __version__:
    print 'Version %s\n' % __version__

  if __authors__:
    print 'Written by %s' % __authors__[0]
    for author in __authors__[1:]:
      print '      and %s' % author
    print

  if __copyright__:
    print __copyright__
    print

  parser = opt_parser()
  options,args = parser.parse_args()

  if __license__ and options.license:
    print __license__
    return

  check_accelerators(__accelerators__)

  if options.help:
    parser.print_help(sys.stdout)
    return

  if not args:
    parser.print_usage(sys.stdout)
    print '''basic options:
      -h, --help            show detailed help on program usage'''
    if __copyright__ or __license__:
      print "      --license             show program's copyright and license terms and exit"
    print
    return

  start = time.clock()
  sys.stderr.write('[%s] Analysis start\n' % time.asctime())

  try:
    run_profile(progmain,options,args)

  except KeyboardInterrupt:
    sys.stderr.write('\n[%s] Analysis aborted by user\n' % time.asctime())

  except TagZillaError, e:
    sys.stderr.write('\n%s\n\n[%s] Analysis aborted due to reported error\n' % (e,time.asctime()))

  except IOError, e:
    sys.stderr.write('\n%s\n\n[%s] Analysis aborted due to input/output error\n' % (e,time.asctime()))

  except:
    import traceback
    sys.stderr.write('''
Analysis aborted due to a problem with the program input, parameters
supplied, an error in the program.  Please examine the following failure
trace for clues as to what may have gone wrong.  When in doubt, please send
this message and a complete description of the analysis you are attempting
to perform to the software developers.

Traceback:
  %s

[%s] Analysis aborted due to unhandled error
''' % (traceback.format_exc().replace('\n','\n  '),time.asctime()))

  else:
    sys.stderr.write('[%s] Analysis completed successfully\n' % time.asctime())
    sys.stderr.write('[%s] CPU time: %s\n' % (time.asctime(),format_elapsed_time(time.clock()-start)))


def main():
  launcher(tagzilla, option_parser, **globals())

if __name__ == '__main__':
  main()
