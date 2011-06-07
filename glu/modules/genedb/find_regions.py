# -*- coding: utf-8 -*-

__gluindex__  = True
__abstract__  = 'Resolve genomic metadata given feature names for SNPs, genes, and bounded regions'
__copyright__ = 'Copyright (c) 2007-2009, BioInformed LLC and the U.S. Department of Health & Human Services. Funded by NCI under Contract N01-CO-12400.'
__license__   = 'See GLU license for terms by running: glu license'
__revision__  = '$Id$'


import sys

from   glu.lib.fileutils          import table_reader,table_writer,tryint

from   glu.modules.genedb         import open_genedb
from   glu.modules.genedb.queries import query_genes_by_name, query_snps_by_name, query_cytoband_by_name, \
                                         query_contig_by_name


HEADER = ['FEATURE_NAME','CHROMOSOME','STRAND','FEATURE_START','FEATURE_END','BASES_UP',
          'BASES_DOWN','SNPS_UP','SNPS_DOWN','FEATURE_TYPE']


def option_parser():
  import optparse

  usage = 'usage: %prog [options] file'
  parser = optparse.OptionParser(usage=usage)

  parser.add_option('-g', '--genedb',   dest='genedb', metavar='NAME',
                      help='Genedb genome annotation database name or file')
  parser.add_option('-u', '--upbases',   dest='upbases',   default=20000, type='int',  metavar='N',
                    help='upstream margin in bases (default=20000)')
  parser.add_option('-d', '--downbases', dest='downbases', default=10000, type='int',  metavar='N',
                    help='downstream margin in bases (default=10000)')
  parser.add_option('-U', '--upsnps',    dest='upsnps',                   type='int',  metavar='N',
                    help='maximum number of upstream SNPs (default=0 for no limit)')
  parser.add_option('-D', '--downsnps',  dest='downsnps',                 type='int',  metavar='N',
                    help='maximum number of downstream SNPs (default=0 for no limit)')
  parser.add_option('-F', '--outformat', dest='outformat', default='GLU',              metavar='NAME',
                    help='Output format (GLU or BED)')
  parser.add_option('-o', '--output',   dest='output', default='-',                    metavar='FILE',
                    help="output file name, '-' for standard out")
  return parser


def coalesce(*items):
  for i in items:
    if i not in ('',None):
      return i
  return None


def is_int(i):
  return isinstance(i, (int,long))


def resolve_feature(con,feature,options):
  name        = feature[0]
  chr         = feature[1] or None
  strand      = feature[2] or '+'
  start       = coalesce(tryint(feature[3]))
  end         = coalesce(tryint(feature[4]))
  upbases     = coalesce(tryint(feature[5]), options.upbases)
  downbases   = coalesce(tryint(feature[6]), options.downbases)
  upsnps      = coalesce(tryint(feature[7]), options.upsnps)
  downsnps    = coalesce(tryint(feature[8]), options.downsnps)

  found = False

  if (start is not None and end is None) or (is_int(start) and is_int(end) and start+1==end):
    feature = 'SNP'
    found = True

  if not found and chr and start and end:
    found = True
    geneinfo = query_genes_by_name(con,name)
    if any( (chr,start,end) == (gi[2],gi[3],gi[4]) for gi in geneinfo):
      feature = 'GENE'
    else:
      feature = 'REGION'

  if not found:
    geneinfo = query_genes_by_name(con,name)
    if len(geneinfo) == 1:
      name,chr,start,end,strand = geneinfo[0][1:6]
      feature = geneinfo[0][6]
      found = True
    elif len(geneinfo) > 1:
      feature = 'AMBIGUOUS GENE'
      found = True

  if not found:
    cytoinfo = query_cytoband_by_name(con,name)
    if cytoinfo:
      chr,start,end,color = cytoinfo
      strand = '+'
      feature = 'CYTOBAND'
      found = True

  if not found:
    contiginfo = query_contig_by_name(con,name)
    if contiginfo:
      chr,start,end = contiginfo
      strand = '+'
      feature = 'CONTIG'
      found = True

  if not found:
    snpinfo = query_snps_by_name(con,name)
    if len(snpinfo)==1:
      name,chr,start,end,strand,refAllele,alleles,vclass,func,weight = snpinfo[0]
      feature = 'SNP'
      found = True

    elif len(snpinfo) > 1:
      feature = 'AMBIGUOUS'
      found = True

  if not found:
    feature = 'UNKNOWN'

  return name,chr,strand,start,end,upbases,downbases,upsnps,downsnps,feature


def resolve_features(con,features,options):
  for feature in features:
    feature += [None]*(10-len(feature))
    yield resolve_feature(con,feature,options)


def bed_format(results):
  for row in results:
    feature   = row[0]
    chrom     = row[1]
    start     = row[3]
    end       = row[4]
    strand    = row[2] or '+'
    upbases   = row[5] or 0
    downbases = row[6] or 0

    if start is None or end is None:
      continue

    if strand=='+':
      start -= upbases
      end   += downbases
    elif strand=='-':
      start -= downbases
      end   += upbases

    yield chrom,start,end,feature


def main():
  parser = option_parser()
  options,args = parser.parse_args()

  if not args:
    parser.print_help(sys.stderr)
    sys.exit(2)

  options.outformat = options.outformat.lower()

  if options.outformat not in ('glu','bed'):
    raise ValueError('Unknown output format selected: %s' % options.outformat)

  con = open_genedb(options.genedb)
  out = table_writer(options.output,hyphen=sys.stdout)

  if options.outformat=='glu':
    out.writerow(HEADER)

  for infile in args:
    features = table_reader(infile,want_header=True,hyphen=sys.stdin)
    results  = resolve_features(con,features,options)

    if options.outformat=='bed':
      results = bed_format(results)

    out.writerows(results)


if __name__=='__main__':
  main()
