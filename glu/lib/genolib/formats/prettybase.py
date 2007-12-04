# -*- coding: utf-8 -*-
'''
File:          prettybase.py

Authors:       Kevin Jacobs (jacobske@bioinformed.com)

Created:       2006-01-01

Abstract:      GLU text genotype format input/output objects

Requires:      Python 2.5

Revision:      $Id$
'''

from __future__ import with_statement

__copyright__ = 'Copyright (c) 2007 Science Applications International Corporation ("SAIC")'
__license__   = 'See GLU license for terms by running: glu license'


import re

from   itertools                 import islice

from   glu.lib.fileutils         import autofile,namefile

from   glu.lib.genolib.streams   import GenotripleStream


__all__ = ['PrettybaseGenotripleWriter', 'save_genotriples_prettybase', 'load_genotriples_prettybase']


def load_genotriples_prettybase(filename,unique=True,limit=None,genome=None):
  '''
  Load genotype triples from file

  @param     filename: file name or file object
  @type      filename: str or file object
  @param     genorepr: function to convert list genotype strings to desired
                       internal representation
  @type      genorepr: unary function
  @param       unique: verify that rows and columns are uniquely labeled
                       (default is True)
  @type        unique: bool
  @param        limit: limit the number of genotypes loaded
  @type         limit: int or None
  @param       genome: genome descriptor
  @type        genome: Genome instance
  @rtype             : GenotripleStream

  >>> from StringIO import StringIO
  >>> data = StringIO('l1 s1 A A\\nl2 s1 G G\\nl1 s2 N N\\nl2 s2 C C\\n')
  >>> triples = load_genotriples_prettybase(data)
  >>> for triple in triples:
  ...   print triple
  ('s1', 'l1', ('A', 'A'))
  ('s1', 'l2', ('G', 'G'))
  ('s2', 'l1', (None, None))
  ('s2', 'l2', ('C', 'C'))
  '''
  re_spaces = re.compile('[\t ,]+')
  gfile = autofile(filename)

  if limit:
    gfile = islice(gfile,limit)

  def _load():
    # Micro-optimization
    split        = re_spaces.split
    local_intern = intern
    local_strip  = str.strip
    amap         = {'N':None,'n':None}

    for line_num,line in enumerate(gfile):
      row = split(local_strip(line))
      if not row:
        continue
      elif len(row) != 4:
        raise ValueError('Invalid prettybase row on line %d of %s' % (line_num+1,namefile(filename)))

      locus  = local_intern(local_strip(row[0]))
      sample = local_intern(local_strip(row[1]))
      a1,a2  = row[2],row[3]
      geno   = amap.get(a1,a1),amap.get(a2,a2)

      yield sample,locus,geno

  return GenotripleStream.from_tuples(_load(),unique=unique,genome=genome)


class PrettybaseGenotripleWriter(object):
  '''
  Object to write genotype triple data to a Prettybase format file

  Genotype triple files must be supplied as and are output to whitespace
  delimited ASCII files as a sequence of four items:

    1. Locus name
    2. Sample name
    3. Allele 1, N for missing
    4. Allele 2, N for missing

  All rows output have exactly these four columns and no file header is
  output. Sample and locus names are arbitrary and user-specified strings.

  >>> triples = [('s1','l1',('C','T')), ('s1','l2',(None,None)),
  ...            ('s1','l3',('A','A')), ('s2','l2', ('C','C'))]
  >>> triples = iter(GenotripleStream.from_tuples(triples))
  >>> from cStringIO import StringIO
  >>> o = StringIO()
  >>> with PrettybaseGenotripleWriter(o) as w:
  ...   w.writerow(*triples.next())
  ...   w.writerow(*triples.next())
  ...   w.writerows(triples)
  >>> print o.getvalue() # doctest: +NORMALIZE_WHITESPACE
  l1 s1 C T
  l2 s1 N N
  l3 s1 A A
  l2 s2 C C
  '''
  def __init__(self,filename):
    '''
    @param     filename: file name or file object
    @type      filename: str or file object
    '''
    self.out = autofile(filename,'w')

  def writerow(self, sample, locus, geno):
    '''
    Write a genotype triple (sample,locus,genotype)

    @param sample: sample identifier
    @type  sample: str
    @param  locus: locus identifier
    @type   locus: str
    @param   geno: genotypes internal representation
    @type    geno: genotype representation
    '''
    out = self.out
    if out is None:
      raise IOError('Cannot write to closed writer object')

    out.write( ' '.join( [locus,sample,geno[0] or 'N',geno[1] or 'N'] ) )
    out.write('\n')

  def writerows(self, triples):
    '''
    Write a genotype sequence of triples (sample,locus,genotype)

    @param  triples: sequence of (sample,locus,genotype)
    @type   triples: sequence of (str,str,genotype representation)
    '''
    out = self.out
    if out is None:
      raise IOError('Cannot write to closed writer object')

    write = out.write
    join  = ' '.join

    for sample,locus,geno in triples:
      write( join( [locus,sample,geno[0] or 'N',geno[1] or 'N'] ) )
      write('\n')

  def close(self):
    '''
    Close the writer.

    A closed writer cannot be used for further I/O operations and will
    result in an error if called more than once.
    '''
    if self.out is None:
      raise IOError('Writer object already closed')
    self.out = None

  def __enter__(self):
    '''
    Context enter function
    '''
    return self

  def __exit__(self, *exc_info):
    '''
    Context exit function that closes the writer upon exit
    '''
    self.close()


def save_genotriples_prettybase(filename,triples):
  '''
  Write the genotype triple data to file.

  @param     filename: file name or file object
  @type      filename: str or file object
  @param      triples: genotype triple data
  @type       triples: sequence

  >>> triples = [ ('s1', 'l1',  ('C','T')),
  ...             ('s1', 'l2', (None,None)),
  ...             ('s1', 'l3',  ('A','A')) ]
  >>> triples = GenotripleStream.from_tuples(triples)
  >>> from cStringIO import StringIO
  >>> o = StringIO()
  >>> save_genotriples_prettybase(o,triples)
  >>> print o.getvalue() # doctest: +NORMALIZE_WHITESPACE
  l1 s1 C T
  l2 s1 N N
  l3 s1 A A
  '''
  with PrettybaseGenotripleWriter(filename) as w:
    w.writerows(triples)


def test():
  import doctest
  return doctest.testmod()


if __name__ == '__main__':
  test()