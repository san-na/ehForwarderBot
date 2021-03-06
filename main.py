import config
import queue
import threading
import logging
import argparse

__version__ = "1.2 build 20170112"

parser = argparse.ArgumentParser(description="EH Forwarder Bot is an extensible chat tunnel framework which allows "
                                             "users to contact people from other chat platforms, and ultimately "
                                             "remotely control their accounts in other platforms.",
                                 epilog="Support: https://github.com/blueset/ehForwarderBot")
parser.add_argument("-v", default=0, action="count",
                    help="Increase verbosity.")
parser.add_argument("-V", "--version", action="version",
                    help="Show version number and exit.",
                    version="EFB Forwarder Bot %s" % __version__)
parser.add_argument("-l", "--log",
                    help="Set log file path.")

args = parser.parse_args()

q = None
slaves = None
master = None
master_thread = None
slave_threads = None


def set_log_file(fn):
    """
    Set log file path for root logger

    Args:
        fn (str): File name
    """
    fh = logging.FileHandler(fn, 'a')
    f = logging.Formatter('%(asctime)s: %(name)s [%(levelname)s]\n    %(message)s')
    fh.setFormatter(f)
    l = logging.getLogger()
    for hdlr in l.handlers[:]:
        l.removeHandler(hdlr)
    l.addHandler(fh)


def init():
    """
    Initialize all channels.
    """
    global q, slaves, master, master_thread, slave_threads
    # Init Queue
    q = queue.Queue()
    # Initialize Plug-ins Library
    # (Load libraries and modules and init them with Queue `q`)
    slaves = {}
    for i in config.slave_channels:
        obj = getattr(__import__(i[0], fromlist=i[1]), i[1])
        slaves[obj.channel_id] = obj(q)
    master = getattr(__import__(config.master_channel[0], fromlist=config.master_channel[1]), config.master_channel[1])(
        q, slaves)

    master_thread = threading.Thread(target=master.poll)
    slave_threads = {key: threading.Thread(target=slaves[key].poll) for key in slaves}


def poll():
    """
    Start threads for polling
    """
    global master_thread, slave_threads
    master_thread.start()
    for i in slave_threads:
        slave_threads[i].start()


PID = "/tmp/efb.pid"
LOG = "EFB.log"

if getattr(args, "V", None):
    print("EH Forwarder Bot\n"
          "Version: %s" % __version__)
else:
    if args.v == 0:
        level = logging.ERROR
    elif args.v == 1:
        level = logging.INFO
    else:
        level = logging.DEBUG
    logging.basicConfig(format='%(asctime)s: %(name)s [%(levelname)s]\n    %(message)s', level=level)
    logging.getLogger('requests').setLevel(logging.CRITICAL)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)
    logging.getLogger('telegram.bot').setLevel(logging.CRITICAL)

    if getattr(args, "log", None):
        LOG = args.log
        set_log_file(LOG)

    init()
    poll()
