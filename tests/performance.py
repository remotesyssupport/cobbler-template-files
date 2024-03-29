# test script to evaluate Cobbler API performance
#
# Michael DeHaan <mdehaan@redhat.com>

import os
import cobbler.api as capi
import time
import sys
import random

N = 1000
print "sample size is %s" % N

api = capi.BootAPI()

# part one ... create our test systems for benchmarking purposes if
# they do not seem to exist.

if not api.profiles().find("foo"):
    print "CREATE A PROFILE NAMED 'foo' to be able to run this test"
    sys.exit(0)

def random_mac():
    mac = [ 0x00, 0x16, 0x3e,
      random.randint(0x00, 0x7f),
      random.randint(0x00, 0xff),
      random.randint(0x00, 0xff) ]
    return ':'.join(map(lambda x: "%02x" % x, mac))

print "Deleting autotest entries from a previous run"
time1 = time.time()
for x in xrange(0,N):
   try:
       sys = api.systems().remove("autotest-%s" % x,with_delete=True)
   except:
       pass
time2 = time.time()
print "ELAPSED: %s seconds" % (time2 - time1)

print "Creating test systems from scratch"
time1 = time.time()
for x in xrange(0,N):
   sys = api.new_system()
   sys.set_name("autotest-%s" % x)
   sys.set_mac_address(random_mac())
   sys.set_profile("foo") # assumes there is already a foo
   # print "... adding: %s" % sys.name
   api.systems().add(sys,save=True,with_sync=False,with_triggers=False)
time2 = time.time()
print "ELAPSED %s seconds" % (time2 - time1)

for mode2 in [ "fast", "normal", "full" ]:
   for mode in [ "on", "off" ]:

       print "Running netboot edit benchmarks (turn %s, %s)" % (mode, mode2)
       time1 = time.time()
       for x in xrange(0,N):
           sys = api.systems().find("autotest-%s" % x)
           if mode == "off":
               sys.set_netboot_enabled(0)
           else:
               sys.set_netboot_enabled(1)
           # print "... editing: %s" % sys.name
           if mode2 == "fast":
               api.systems().add(sys, save=True, with_sync=False, with_triggers=False, quick_pxe_update=True)
           if mode2 == "normal":
               api.systems().add(sys, save=True, with_sync=False, with_triggers=False)
           if mode2 == "full":
               api.systems().add(sys, save=True, with_sync=True, with_triggers=True)

       time2 = time.time()
       print "ELAPSED: %s seconds" % (time2 - time1)



