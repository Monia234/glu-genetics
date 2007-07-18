'''
Author: Vincent Nijs (+ ?)

Email: v-nijs at kellogg.northwestern.edu

Last Modified: Mon Jan 15 17:56:17 CST 2007

Dependencies: See import statement at the top of this file
'''

from __future__     import division

import time

from   itertools    import izip

from   scipy        import c_, ones, dot, stats, diff
from   scipy.linalg import inv, solve, det
from   numpy        import log, pi, sqrt, square, diagonal
from   numpy.random import randn, seed

class ols(object):
  '''
  Class for multi-variate regression using OLS

  For usage examples of other class methods see the class tests at the
  bottom of this file. To see the class in action simply run this file using
  'python ols.py'. This will generate some simulated data and run various
  analyses. If you have rpy installed the same model will also be estimated
  by R for confirmation.

  Input:
    y = dependent variable
    y_varnm = string with the variable label for y
    x = independent variables, note that a constant is added by default
    x_varnm = string or list of variable labels for the independent variables

  Output:
    There are no values returned by the class. Summary provides printed output.
    All other measures can be accessed as follows:

    Step 1: Create an OLS instance by passing data to the class

      m = ols(y,x,y_varnm = 'y',x_varnm = ['x1','x2','x3','x4'])

    Step 2: Get specific metrics

      To print the coefficients:
        >>> print m.b
      To print the coefficients p-values:
        >>> print m.p
  '''

  def __init__(self,y,x,y_varnm = 'y',x_varnm = ''):
    '''
    Initializing the ols class.
    '''
    self.y = y
    self.x = c_[ones(x.shape[0]),x]
    self.y_varnm = y_varnm
    self.x_varnm = ['const'] + list(x_varnm)

    # Estimate model using OLS
    self.estimate()

  def estimate(self):

    # estimating coefficients, and basic stats
    self.inv_xx = inv(dot(self.x.T,self.x))
    xy          = dot(self.x.T,self.y)
    self.b      = dot(self.inv_xx,xy)                        # estimate coefficients

    self.nobs   = self.y.shape[0]                            # number of observations
    self.ncoef  = self.x.shape[1]                            # number of coef.
    self.df_e   = self.nobs - self.ncoef                     # degrees of freedom, error
    self.df_r   = self.ncoef - 1                             # degrees of freedom, regression

    self.e      = self.y - dot(self.x,self.b)                # residuals
    self.sse    = dot(self.e,self.e)/self.df_e               # SSE
    self.se     = sqrt(diagonal(self.sse*self.inv_xx))       # coef. standard errors
    self.t      = self.b / self.se                           # coef. t-statistics
    self.p      = 2*stats.distributions.t.sf(abs(self.t), self.df_e)       # coef. p-values

    self.R2     = 1 - self.e.var()/self.y.var()                        # model R-squared
    self.R2adj  = 1-(1-self.R2)*((self.nobs-1)/(self.nobs-self.ncoef)) # adjusted R-square

    self.F      = self.R2/(1-self.R2)*self.df_e/self.df_r       # model F-statistic
    self.Fpv    = stats.distributions.f.sf(self.F, self.df_r, self.df_e)      # F-statistic p-value

  def dw(self):
    '''
    Calculates the Durbin-Waston statistic
    '''
    de = diff(self.e,1)
    dw = dot(de,de) / dot(self.e,self.e)

    return dw

  def omni(self):
    '''
    Omnibus test for normality
    '''
    return stats.normaltest(self.e)

  def JB(self):
    '''
    Calculate residual skewness, kurtosis, and do the Jarque-Bera test for normality
    '''

    # Calculate residual skewness and kurtosis
    skew = stats.skew(self.e)
    kurtosis = 3 + stats.kurtosis(self.e)

    # Calculate the Jarque-Bera test for normality
    JB = (self.nobs/6) * (square(skew) + (1/4)*square(kurtosis-3))
    JBpv = stats.distributions.chi2.sf(JB,2)

    return JB, JBpv, kurtosis, skew

  def ll(self):
    '''
    Calculate model log-likelihood and two information criteria
    '''

    # Model log-likelihood, AIC, and BIC criterion values
    ll = -(self.nobs*1/2)*(1+log(2*pi)) - (self.nobs/2)*log(dot(self.e,self.e)/self.nobs)
    aic = -2*ll/self.nobs + (2*self.ncoef/self.nobs)
    bic = -2*ll/self.nobs + (self.ncoef*log(self.nobs))/self.nobs

    return ll, aic, bic

  def summary(self):
    '''
    Printing model output to screen
    '''
    # local time & date
    t = time.localtime()

    # extra stats
    ll, aic, bic = self.ll()
    JB, JBpv, skew, kurtosis = self.JB()
    omni, omnipv = self.omni()

    # printing output to screen
    print '\n=============================================================================='
    print "Dependent Variable: " + self.y_varnm
    print "Method: Least Squares"
    print "Date: ", time.strftime("%a, %d %b %Y",t)
    print "Time: ", time.strftime("%H:%M:%S",t)
    print '# obs:          %5.0f' % self.nobs
    print '# variables:    %5.0f' % self.ncoef
    print '=============================================================================='
    print 'variable    coefficient    std. error   t-statistic    p value'
    print '=============================================================================='
    for x in izip(self.x_varnm,self.b,self.se,self.t,self.p):
      print '%-10s %12.6f  %12.6f  %12.6f    %8.6f' % x
    print '=============================================================================='
    print 'Models stats                          Residual stats'
    print '=============================================================================='
    print 'R-squared           %12.6f      Durbin-Watson stat  %12.6f' % (self.R2, self.dw())
    print 'Adjusted R-squared  %12.6f      Omnibus stat        %12.6f' % (self.R2adj, omni)
    print 'F-statistic         %12.6f      Prob(Omnibus stat)  %12.6f' % (self.F, omnipv)
    print 'Prob (F-statistic)  %12.6f      Jarque-Bera stat    %12.6f' % (self.Fpv, JB)
    print 'Log likelihood    %14.6f      Prob(Jarque-Bera)   %12.6f' % (ll, JBpv)
    print 'AIC criterion       %12.6f      Skew                %12.6f' % (aic, skew)
    print 'BIC criterion       %12.6f      Kurtosis            %12.6f' % (bic, kurtosis)
    print '=============================================================================='


if __name__ == '__main__':

  ##########################
  ### testing the ols class
  ##########################

  # creating simulated data and variable labels
  seed(1)
  data =  randn(1000,5)      # the data array

  # intercept is added, by default
  m = ols(data[:,0],data[:,1:],y_varnm = 'y',x_varnm = ['x1','x2','x3','x4'])
  m.summary()

  # if you have rpy installed, use it to test the results
  have_rpy =  False
  try:
    print "\n"
    print "="*30
    print "Validating OLS results in R"
    print "="*30

    import rpy
    have_rpy = True
  except ImportError:
    print "\n"
    print "="*30
    print "Validating OLS-class results in R"
    print "="*30
    print "rpy is not installed"
    print "="*30

  if have_rpy:
    y  = data[:,0]
    x1 = data[:,1]
    x2 = data[:,2]
    x3 = data[:,3]
    x4 = data[:,4]
    rpy.set_default_mode(rpy.NO_CONVERSION)
    linear_model = rpy.r.lm(rpy.r("y ~ x1 + x2 + x3 + x4"), data = rpy.r.data_frame(x1=x1,x2=x2,x3=x3,x4=x4,y=y))
    rpy.set_default_mode(rpy.BASIC_CONVERSION)
    print linear_model.as_py()['coefficients']
    summary = rpy.r.summary(linear_model)
    print summary
