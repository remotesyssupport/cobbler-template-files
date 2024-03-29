"""
Enables the "cobbler import" command to seed cobbler
information with available distribution from rsync mirrors
and mounted DVDs.  

Copyright 2006-2008, Red Hat, Inc
Michael DeHaan <mdehaan@redhat.com>

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
02110-1301  USA
"""

from cexceptions import *
import os
import os.path
import traceback
import sub_process
import glob
import api
import utils
import shutil
from utils import _

WGET_CMD = "wget --mirror --no-parent --no-host-directories --directory-prefix %s/%s %s"
RSYNC_CMD =  "rsync -a %s '%s' %s/ks_mirror/%s --exclude-from=/etc/cobbler/rsync.exclude --progress"

TRY_LIST = [
   "pool",
   "Fedora", "Packages", "RedHat", "Client", "Server", "Centos", "CentOS",
   "Fedora/RPMS", "RedHat/RPMS", "Client/RPMS", "Server/RPMS", "Centos/RPMS",
   "CentOS/RPMS", "RPMS"
]

class Importer:

   def __init__(self,api,config,mirror,mirror_name,network_root=None,kickstart_file=None,rsync_flags=None,arch=None,breed=None):
       """
       Performs an import of a install tree (or trees) from the given
       mirror address.  The prefix of the distro is to be specified
       by mirror name.  For instance, if FC-6 is given, FC-6-xen-i386
       would be a potential distro that could be created.  For content
       available on external servers via a known nfs:// or ftp:// or
       http:// path, we can import without doing rsync mirorring to 
       cobbler's http directory.  This is explained in more detail 
       in the manpage.  Leave network_root to None if want mirroring.
       """
       self.api = api
       self.config = config
       self.mirror = mirror
       self.mirror_name = mirror_name
       self.network_root = network_root 
       self.distros  = config.distros()
       self.profiles = config.profiles()
       self.systems  = config.systems()
       self.settings = config.settings()
       self.distros_added = []
       self.kickstart_file = kickstart_file
       self.rsync_flags = rsync_flags
       self.arch = arch
       self.breed = breed

   # ----------------------------------------------------------------------

   def run(self):
       if self.mirror is None:
           raise CX(_("import failed.  no --mirror specified"))
       if self.mirror_name is None:
           raise CX(_("import failed.  no --name specified"))
       if self.arch is not None:
           self.arch = self.arch.lower()
           if self.arch == "x86":
               # be consistent
               self.arch = "i386"
           if self.arch not in [ "i386", "ia64", "x86_64", "s390x" ]:
               raise CX(_("arch must be i386, ia64, x86_64, or s390x"))

       mpath = os.path.join(self.settings.webdir, "ks_mirror", self.mirror_name)

       if os.path.exists(mpath) and self.arch is None:
           # FIXME : Raise exception even when network_root is given ?
           raise CX(_("Something already exists at this import location (%s).  You must specify --arch to avoid potentially overwriting existing files.") % mpath)
 
       if self.arch:
           # append the arch path to the name if the arch is not already
           # found in the name.
           for x in [ "ia64", "i386", "x86_64", "x86", "s390x" ]:
               if self.mirror_name.lower().find(x) != -1:
                   if self.arch != x :
                       raise CX(_("Architecture found on pathname (%s) does not fit the one given in command line (%s)")%(x,self.arch))
                   break
           else:
               # FIXME : This is very likely removed later at get_proposed_name, and the guessed arch appended again
               self.mirror_name = self.mirror_name + "-" + self.arch

       # make the output path and mirror content but only if not specifying that a network
       # accessible support location already exists

       if self.network_root is None:
           self.path = "%s/ks_mirror/%s" % (self.settings.webdir, self.mirror_name)
           self.mkdir(self.path)

           # prevent rsync from creating the directory name twice
           if not self.mirror.endswith("/"):
               self.mirror = "%s/" % self.mirror 

           if self.mirror.startswith("http://") or self.mirror.startswith("ftp://") or self.mirror.startswith("nfs://"):
               # http mirrors are kind of primative.  rsync is better.
               # that's why this isn't documented in the manpage.
               # TODO: how about adding recursive FTP as an option?
               raise CX(_("unsupported protocol"))
           else:
               # use rsync.. no SSH for public mirrors and local files.
               # presence of user@host syntax means use SSH
               spacer = ""
               if not self.mirror.startswith("rsync://") and not self.mirror.startswith("/"):
                   spacer = ' -e "ssh" '
               rsync_cmd = RSYNC_CMD
               if self.rsync_flags:
                   rsync_cmd = rsync_cmd + " " + self.rsync_flags
               self.run_this(rsync_cmd, (spacer, self.mirror, self.settings.webdir, self.mirror_name))

       # see that the root given is valid

       if self.network_root is not None:
           if not os.path.exists(self.mirror):
               raise CX(_("path does not exist: %s") % self.mirror)

           if not self.network_root.endswith("/"):
               self.network_root = self.network_root + "/"
           self.path = self.mirror
           valid_roots = [ "nfs://", "ftp://", "http://" ]
           for valid_root in valid_roots:
               if self.network_root.startswith(valid_root):
                   break
           else:
               raise CX(_("Network root given to --available-as must be nfs://, ftp://, or http://"))
           if self.network_root.startswith("nfs://"):
               try:
                   (a,b,rest) = self.network_root.split(":",3)
               except:
                   raise CX(_("Network root given to --available-as is missing a colon, please see the manpage example."))

       self.processed_repos = {}

       print _("---------------- (adding distros)")
       os.path.walk(self.path, self.distro_adder, {})

       if self.network_root is None:
           print _("---------------- (associating repos)")
           # FIXME: this automagic is not possible (yet) without mirroring 
           self.repo_finder()

       print _("---------------- (associating kickstarts)")
       self.kickstart_finder() 

       print _("---------------- (syncing)")
       self.api.sync()

       return True

   # ----------------------------------------------------------------------

   def mkdir(self, dir):
       try:
           os.makedirs(dir)
       except OSError , ex:
           if ex.strerror == "Permission denied":
               raise CX(_("Permission denied at %s")%dir)
       except:
           pass

   # ----------------------------------------------------------------------

   def run_this(self, cmd, args):
       my_cmd = cmd % args
       print _("- %s") % my_cmd
       rc = sub_process.call(my_cmd,shell=True)
       if rc != 0:
          raise CX(_("Command failed"))

   # ----------------------------------------------------------------------

   def kickstart_finder(self):
       """
       For all of the profiles in the config w/o a kickstart, use the
       given kickstart file, or look at the kernel path, from that, 
       see if we can guess the distro, and if we can, assign a kickstart 
       if one is available for it.
       """

       for profile in self.profiles:
           distro = self.distros.find(name=profile.distro)
           if distro is None or not (distro in self.distros_added):
               # print _("- skipping distro %s since it wasn't imported this time") % profile.distro
               continue

           if self.kickstart_file == None:
               kdir = os.path.dirname(distro.kernel)   
               # FIXME : Some special handling for network_root might be required as in get_proposed_name,
               base_dir = "/".join(kdir.split("/")[:6])
          
               for try_entry in TRY_LIST:
                   try_dir = os.path.join(base_dir, try_entry)
                   if os.path.exists(try_dir):
                       rpms = glob.glob(os.path.join(try_dir, "*release-*"))
                       for rpm in rpms:
                           if rpm.find("notes") != -1:
                               continue
                           results = self.scan_rpm_filename(rpm)
                           if results is None:
                               continue
                           (flavor, major, minor) = results
                           # print _("- finding default kickstart template for %(flavor)s %(major)s") % { "flavor" : flavor, "major" : major }
                           kickstart = self.set_variance(profile, flavor, major, minor, distro)
                       if not rpms:
                           # search for base-files or base-installer ?
                           rpms = glob.glob(os.path.join(try_dir, "main/b/base-files" , "base-files_*"))
                           for rpm in rpms:
                               results = self.scan_deb_filename(rpm)
                               if results is None:
                                   continue
                               (flavor, major, minor) = results
                               if self.breed and self.breed != flavor:
                                   raise CX(_("Error: given breed does not match imported tree"))
                               # print _("- finding default kickstart template for %(flavor)s %(major)s") % { "flavor" : flavor, "major" : major }
                               kickstart = self.set_variance(profile, flavor, major, minor, distro)
           else:
               # FIXME : Why not fix this while initially creating the profile ?
               print _("- using kickstart file %s") % self.kickstart_file
               profile.set_kickstart(self.kickstart_file)

           self.configure_tree_location(distro)
           self.distros.add(distro,save=True) # re-save
           self.api.serialize()

   # --------------------------------------------------------------------

   def configure_tree_location(self, distro):
       # find the tree location
       dirname = os.path.dirname(distro.kernel)
       tokens = dirname.split("/")
       tokens = tokens[:-2]
       base = "/".join(tokens)
       dest_link = os.path.join(self.settings.webdir, "links", distro.name)

       # create the links directory only if we are mirroring because with
       # SELinux Apache can't symlink to NFS (without some doing)

       if self.network_root is None:
           if not os.path.exists(dest_link):
               try:
                   os.symlink(base, dest_link)
               except:
                   # this shouldn't happen but I've seen it ... debug ...
                   print _("- symlink creation failed: %(base)s, %(dest)s") % { "base" : base, "dest" : dest_link }

           # FIXME: looks like "base" isn't used later.  remove?
           base = base.replace(self.settings.webdir,"")
       
       meta = distro.ks_meta

       # how we set the tree depends on whether an explicit network_root was specified
       if self.network_root is None:
           meta["tree"] = "http://@@http_server@@/cblr/links/%s" % (distro.name)
       else:
           # where we assign the kickstart source is relative to our current directory
           # and the input start directory in the crawl.  We find the path segments
           # between and tack them on the network source path to find the explicit
           # network path to the distro that Anaconda can digest.  
           tail = self.path_tail(self.mirror, base)
           meta["tree"] = self.network_root
           if meta["tree"].endswith("/"):
              meta["tree"] = self.network_root[:-1]
           meta["tree"] = meta["tree"] + tail.rstrip()

       # print _("- tree: %s") % meta["tree"]
       distro.set_ksmeta(meta)

   # ---------------------------------------------------------------------

   def path_tail(self, apath, bpath):
       """ 
       Given two paths (B is longer than A), find the part in B not in A
       """
       position = bpath.find(apath)
       if position != 0:
           print "%s, %s, %s" % (apath, bpath, position)
           #raise CX(_("Error: possible symlink traversal?: %s") % bpath)
           print _("- warning: possible symlink traversal?: %s") % bpath
       rposition = position + len(self.mirror)
       result = bpath[rposition:]
       if not result.startswith("/"):
           result = "/" + result
       return result

   # ---------------------------------------------------------------------

   def set_variance(self, profile, flavor, major, minor, distro):
  
       # find the profile kickstart and set the distro breed/os-version based on what
       # we can find out from the rpm filenames and then return the kickstart
       # path to use.

       if flavor == "fedora":

           # this may actually fail because the libvirt/virtinst database
           # is not always up to date.  We keep a simplified copy of this
           # in codes.py.  If it fails we set it to something generic
           # and don't worry about it.
           distro.set_breed("redhat")
           try:
               distro.set_os_version("fedora%s" % int(major))
           except:
               print "- warning: could not store os-version fedora%s" % int(major)
               distro.set_os_version("other")

           if major >= 8:
                return profile.set_kickstart("/etc/cobbler/sample_end.ks")
           if major >= 6:
                return profile.set_kickstart("/etc/cobbler/sample.ks") 

       if flavor == "redhat" or flavor == "centos":
           distro.set_breed("redhat")
           if major <= 2:
                # rhel2.1 is the only rhel2
                distro.set_os_version("rhel2.1")
           else:
                try:
                    distro.set_os_version("rhel%s" % int(major))
                except:
                    print "- warning: could not store os-version %s" % int(major)
                    distro.set_os_version("other")

           if major >= 5:
                return profile.set_kickstart("/etc/cobbler/sample.ks")

       if flavor == "debian":

           distro.set_breed("debian")
           dist_names = { '4.0' : "Etch" , '5.0' : "Lenny" }
           dist_vers = "%s.%s" % ( major , minor )
           distro.set_os_version("debian%s" % dist_names[dist_vers])
           return profile.set_kickstart("/etc/cobbler/sample.seed")

       if flavor == "ubuntu":

           distro.set_breed("ubuntu")
           # Release names taken from wikipedia
           dist_names = { '4.10':"WartyWarthog", '5.4':"HoaryHedgehog", '5.10':"BreezyBadger", '6.4':"DapperDrake", '6.10':"EdgyEft", '7.4':"FeistyFawn", '7.10':"GutsyGibbon", '8.4':"HardyHeron", '8.10':"IntrepidIbex", '9.4':"JauntyJackalope" }
           dist_vers = "%s.%s" % ( major , minor )
           distro.set_os_version("ubuntu%s" % dist_names[dist_vers])
           return profile.set_kickstart("/etc/cobbler/sample.seed")

       print _("- using default kickstart file choice")
       return profile.set_kickstart("/etc/cobbler/legacy.ks")

   # ---------------------------------------------------------------------

   def scan_rpm_filename(self, rpm):
       """
       Determine what the distro is based on the release RPM filename.
       """

       rpm = os.path.basename(rpm)

       # if it looks like a RHEL RPM we'll cheat.
       # it may be slightly wrong, but it will be close enough
       # for RHEL5 we can get it exactly.
       
       for x in [ "4AS", "4ES", "4WS" ]:
          if rpm.find(x) != -1:
             return ("redhat", 4, 0)
       for x in [ "3AS", "3ES", "3WS" ]:
          if rpm.find(x) != -1:
             return ("redhat", 3, 0)
       for x in [ "2AS", "2ES", "2WS" ]:
          if rpm.find(x) != -1:
             return ("redhat", 2, 0)

       # now get the flavor:
       flavor = "redhat"
       if rpm.lower().find("fedora") != -1:
          flavor = "fedora"
       if rpm.lower().find("centos") != -1:
          flavor = "centos"

       # get all the tokens and try to guess a version
       accum = []
       tokens = rpm.split(".")
       for t in tokens:
          tokens2 = t.split("-")
          for t2 in tokens2:
             try:
                 float(t2)
                 accum.append(t2)
             except:
                 pass

       major = float(accum[0])
       minor = float(accum[1])
       return (flavor, major, minor)

   def scan_deb_filename(self, deb):
       """
       Determine what the distro is based on the base-files dpkg filename.
       """

       deb = os.path.basename(deb)

       # now get the flavor:
       flavor = "debian"
       if deb.lower().find("ubuntu") != -1:
          flavor = "ubuntu"

       # get all the tokens and try to guess a version
       accum = []
       tokens = deb.split("_")
       tokens2 = tokens[1].split(".")
       for t2 in tokens2:
          try:
              val = int(t2)
              accum.append(val)
          except:
              pass
       if flavor == "ubuntu":
          accum.pop(0)
          accum.pop(0)
       accum.append(0)

       return (flavor, accum[0], accum[1])

   # ----------------------------------------------------------------------

   def distro_adder(self,foo,dirname,fnames):
       
       initrd = None
       kernel = None
       
       for x in fnames:

           fullname = os.path.join(dirname,x)
           if os.path.islink(fullname) and os.path.isdir(fullname):
              if os.path.realpath(fullname) == os.path.realpath(dirname):
                if not self.breed:
                  self.breed = x
                elif self.breed != x:
                  print "- WARNING - symlink name (%s) does not fit the breed (%s)" % ( x , self.breed )
                continue
              print "- following symlink: %s" % fullname
              os.path.walk(fullname, self.distro_adder, {})

           if x.startswith("initrd"):
               initrd = os.path.join(dirname,x)
           if x.startswith("vmlinuz") or x.startswith("kernel.img"):
               # This ugly trick is to avoid inclusion of powerpc kernels from debian distros
               if x.find("initrd") != -1:
                   continue
               kernel = os.path.join(dirname,x)
           if initrd is not None and kernel is not None and dirname.find("isolinux") == -1:
               self.add_entry(dirname,kernel,initrd)
               # The values are reset because debian media has extra initrd images
               initrd = None
               kernel = None



   # ----------------------------------------------------------------------
   
   def repo_finder(self):
       
       for distro in self.distros_added:
           print _("- traversing distro %s") % distro.name
           if distro.kernel.find("ks_mirror") != -1:
               basepath = os.path.dirname(distro.kernel)
               # FIXME : Some special handling for network_root might be required as in get_proposed_name,
               top = "/".join(basepath.split("/")[:6])
               print _("- descent into %s") % top
               os.path.walk(top, self.repo_scanner, distro)
           else:
               print _("- this distro isn't mirrored")

   # ----------------------------------------------------------------------

   def repo_scanner(self,distro,dirname,fnames):
       
       matches = {} 
       print "- processing: %s" % dirname
       for x in fnames:
          if x == "base" or x == "repodata":
               # only run the repo scanner on directories that contain a comps.xml
               gloob1 = glob.glob("%s/%s/*comps*.xml" % (dirname,x))
               if len(gloob1) >= 1:
                   if matches.has_key(dirname):
                       print _("- looks like we've already scanned here: %s") % dirname
                       continue
                   print _("- need to process repo/comps: %s") % dirname
                   self.process_comps_file(dirname, distro)
                   matches[dirname] = 1
               else:
                   print _("- directory %s is missing xml comps file, skipping") % dirname
                   continue

   # ----------------------------------------------------------------------
               

   def process_comps_file(self, comps_path, distro):

       # all of this is mainly to set up the core repos in a sane
       # way and shouldn't fail if the tree structure is too foreign
       masterdir = "repodata"
       if not os.path.exists(os.path.join(comps_path, "repodata")):
           # older distros...
           masterdir = "base"

       print _("- scanning: %(path)s (distro: %(name)s)") % { "path" : comps_path, "name" : distro.name }

       # figure out what our comps file is ...
       print _("- looking for %(p1)s/%(p2)s/*comps*.xml") % { "p1" : comps_path, "p2" : masterdir }
       files = glob.glob("%s/%s/*comps*.xml" % (comps_path, masterdir))
       if len(files) == 0:
           print _("- no comps found here: %s") % os.path.join(comps_path, masterdir)
           return # no comps xml file found

       # pull the filename from the longer part
       comps_file = files[0].split("/")[-1]

       try:

           # store the yum configs on the filesystem so we can use them later.
           # and configure them in the kickstart post, etc

           print "- possible source repo match"
           counter = len(distro.source_repos)

           # find path segment for yum_url (changing filesystem path to http:// trailing fragment)
           seg = comps_path.rfind("ks_mirror")
           urlseg = comps_path[seg+10:]
           print "- segment: %s" % urlseg

           # write a yum config file that shows how to use the repo.
           if counter == 0:
               dotrepo = "%s.repo" % distro.name
           else:
               dotrepo = "%s-%s.repo" % (distro.name, counter)

           fname = os.path.join(self.settings.webdir, "ks_mirror", "config", "%s-%s.repo" % (distro.name, counter))

           repo_url = "http://@@http_server@@/cobbler/ks_mirror/config/%s-%s.repo" % (distro.name, counter)
         
           repo_url2 = "http://@@http_server@@/cobbler/ks_mirror/%s" % (urlseg) 

           distro.source_repos.append([repo_url,repo_url2])

           # NOTE: the following file is now a Cheetah template, so it can be remapped
           # during sync, that's why we have the @@http_server@@ left as templating magic.
           # repo_url2 is actually no longer used. (?)

           print _("- url: %s") % repo_url
           config_file = open(fname, "w+")
           config_file.write("[core-%s]\n" % counter)
           config_file.write("name=core-%s\n" % counter)
           config_file.write("baseurl=http://@@http_server@@/cobbler/ks_mirror/%s\n" % (urlseg))
           config_file.write("enabled=1\n")
           config_file.write("gpgcheck=0\n")
           config_file.write("priority=1\n")
           config_file.close()

           # don't run creatrepo twice -- this can happen easily for Xen and PXE, when
           # they'll share same repo files.
           if not self.processed_repos.has_key(comps_path):
               utils.remove_yum_olddata(comps_path)
               #cmd = "createrepo --basedir / --groupfile %s %s" % (os.path.join(comps_path, masterdir, comps_file), comps_path)
               cmd = "createrepo -c cache --groupfile %s %s" % (os.path.join(comps_path, masterdir, comps_file), comps_path)
               print _("- %s") % cmd
               sub_process.call(cmd,shell=True)
               self.processed_repos[comps_path] = 1
               # for older distros, if we have a "base" dir parallel with "repodata", we need to copy comps.xml up one...
               p1 = os.path.join(comps_path, "repodata", "comps.xml")
               p2 = os.path.join(comps_path, "base", "comps.xml")
               if os.path.exists(p1) and os.path.exists(p2):
                   print _("- cp %(p1)s %(p2)s") % { "p1" : p1, "p2" : p2 }
                   shutil.copyfile(p1,p2)

       except:
           print _("- error launching createrepo, ignoring...")
           traceback.print_exc()
        

   def add_entry(self,dirname,kernel,initrd):
       for pxe_arch in self.get_pxe_arch(dirname):
           name = self.get_proposed_name(dirname, pxe_arch)

           existing_distro = self.distros.find(name=name)

           if existing_distro is not None:
               raise CX(_("Distro %s does already exists") % name)
               print _("- modifying existing distro: %s") % name
               distro = existing_distro
           else:
               print _("- creating new distro: %s") % name
               distro = self.config.new_distro()
           
           distro.set_name(name)
           distro.set_kernel(kernel)
           distro.set_initrd(initrd)
           distro.set_arch(pxe_arch)
           if self.breed:
               distro.set_breed(self.breed)
           distro.source_repos = []
           self.distros.add(distro,save=True)
           self.distros_added.append(distro)       

           existing_profile = self.profiles.find(name=name) 

           if existing_profile is None:
               print _("- creating new profile: %s") % name 
               profile = self.config.new_profile()
           else:
               raise CX(_("Profile %s does already exists") % name)
               print _("- modifying existing profile: %s") % name
               profile = existing_profile

           profile.set_name(name)
           profile.set_distro(name)
           #if self.kickstart_file:
           #    profile.set_kickstart(self.kickstart_file)
           if name.find("-xen") != -1:
               profile.set_virt_type("xenpv")
           else:
               profile.set_virt_type("qemu")

           self.profiles.add(profile,save=True)

           # Create a rescue image as well,
           # assuming this isn't a xen distro
           if name.find("-xen") == -1:
               rescue_name = 'rescue-' + name
               existing_profile = self.profiles.find(name=rescue_name)

               if existing_profile is None:
                   print _("- creating new profile: %s") % rescue_name
                   profile = self.config.new_profile()
               else:
                   print _("- modifying existing profile: %s") % rescue_name
                   profile = existing_profile

               profile.set_name(rescue_name)
               profile.set_distro(name)
               profile.set_virt_type("qemu")
               profile.kernel_options['rescue'] = None
               profile.kickstart = '/etc/cobbler/pxerescue.ks'

               self.profiles.add(profile,save=True)

           self.api.serialize()

   def get_proposed_name(self,dirname,pxe_arch):
       archname = pxe_arch
       if archname == "x86":
          # be consistent
          archname = "i386"
       # FIXME: this is new, needs testing ...
       if self.network_root is not None:
          name = "-".join(self.path_tail(self.path,dirname).split("/"))
       else:
          # remove the part that says /var/www/cobbler/ks_mirror/name
          name = "-".join(dirname.split("/")[6:])
       if name.startswith("-"):
          name = name[1:]
       name = self.mirror_name + "-" + name
       # FIXME : Why do we clean all these suffixes ?
       name = name.replace("-os","")
       name = name.replace("-images","")
       name = name.replace("-tree","")
       name = name.replace("var-www-cobbler-", "")
       name = name.replace("ks_mirror-","")
       name = name.replace("-pxeboot","")  
       name = name.replace("-install","")  
       name = name.replace("--","-")
       for separator in [ '-' , '_'  , '.' ] :
         for arch in [ "i386" , "x86_64" , "ia64" , "x86" , "s390x" , "386" , "amd" ]:
           name = name.replace("%s%s" % ( separator , arch ),"")
       # ensure arch is on the end, regardless of path used.
       name = name + "-" + archname

       return name

   def arch_walker(self,foo,dirname,fnames):
       """
       See docs on learn_arch_from_tree
       """
 
       # don't care about certain directories
       for x in TRY_LIST:
           if dirname.find(x) != -1:
               break
       else:
          return

       # try to find a kernel header RPM and then look at it's arch.
       for x in fnames:
           if not x.endswith("rpm") and not x.endswith("deb"):
               continue
           if x.find("kernel-header") != -1 or x.find("linux-headers-") != -1:
               print _("- kernel header found: %s") % x
               for arch in [ "i386" , "x86_64" , "ia64" , "s390x" ]:
                   if x.find(arch) != -1:
                       foo[arch] = 1
               if x.find("amd64") != -1:
                   foo["x86_64"] = 1
               if x.find("i686") != -1:
                   foo["i386"] = 1

       if foo.keys():
          return

       # This extra code block is a temporary fix for rhel4.x 64bit [x86_64]
       # distro ARCH identification-- L.M.
       # NOTE: eventually refactor to merge in with the above block
       for x in fnames:
          if not x.endswith("rpm") and not x.endswith("deb"):
             continue
          if x.find("kernel-largesmp") != -1 or x.find("kernel-hugemem") != -1 or x.find("linux-headers-") != -1:
             print _("- kernel header found: %s") % x
             for arch in [ "i386" , "x86_64" , "ia64" , "s390x" ]:
                 if x.find(arch) != -1:
                    foo[arch] = 1
             if x.find("amd64") != -1:
                foo["x86_64"] = 1
             if x.find("i686") != -1:
                foo["i386"] = 1

   def learn_arch_from_tree(self,dirname):
       """ 
       If a distribution is imported from DVD, there is a good chance the path doesn't contain the arch
       and we should add it back in so that it's part of the meaningful name ... so this code helps
       figure out the arch name.  This is important for producing predictable distro names (and profile names)
       from differing import sources
       """
       # FIXME : Some special handling for network_root might be required as in get_proposed_name,
       dirname2 = "/".join(dirname.split("/")[:6])
       print _("- scanning %s for architecture info") % dirname2
       result = {}
       os.path.walk(dirname2, self.arch_walker, result)      
       return result.keys()

   def get_pxe_arch(self,dirname):
       t = dirname.lower()
       if t.find("x86_64") != -1 or t.find("amd") != -1:
          return [ "x86_64" ]
       if t.find("ia64") != -1:
          return [ "ia64" ]
       if t.find("i386") != -1 or t.find("386") != -1 or t.find("x86") != -1:
          return [ "i386" ]
       if t.find("s390") != -1:
          return [ "s390x" ]
       return self.learn_arch_from_tree(dirname)

