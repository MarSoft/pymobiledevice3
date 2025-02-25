import logging
import posixpath
import time
from typing import Generator, List

from cmd2 import Cmd2ArgumentParser, with_argparser
from pycrashreport.crash_report import get_crash_report_from_buf

from pymobiledevice3.exceptions import AfcException
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.lockdown_service_provider import LockdownServiceProvider
from pymobiledevice3.services.afc import AfcService, AfcShell
from pymobiledevice3.services.os_trace import OsTraceService

SYSDIAGNOSE_PROCESS_NAMES = ('sysdiagnose', 'sysdiagnosed')

# on iOS17, we need to wait for a moment before tryint to fetch the sysdiagnose archive
IOS17_SYSDIAGNOSE_DELAY = 1


class CrashReportsManager:
    COPY_MOBILE_NAME = 'com.apple.crashreportcopymobile'
    RSD_COPY_MOBILE_NAME = 'com.apple.crashreportcopymobile.shim.remote'

    CRASH_MOVER_NAME = 'com.apple.crashreportmover'
    RSD_CRASH_MOVER_NAME = 'com.apple.crashreportmover.shim.remote'

    APPSTORED_PATH = '/com.apple.appstored'
    IN_PROGRESS_SYSDIAGNOSE_EXTENSIONS = ['.tmp', '.tar.gz']

    def __init__(self, lockdown: LockdownServiceProvider):
        self.logger = logging.getLogger(__name__)
        self.lockdown = lockdown

        if isinstance(lockdown, LockdownClient):
            self.copy_mobile_service_name = self.COPY_MOBILE_NAME
            self.crash_mover_service_name = self.CRASH_MOVER_NAME
        else:
            self.copy_mobile_service_name = self.RSD_COPY_MOBILE_NAME
            self.crash_mover_service_name = self.RSD_CRASH_MOVER_NAME

        self.afc = AfcService(lockdown, service_name=self.copy_mobile_service_name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self) -> None:
        self.afc.close()

    def clear(self) -> None:
        """
        Clear all crash reports.
        """
        undeleted_items = []
        for filename in self.ls('/'):
            undeleted_items.extend(self.afc.rm(filename, force=True))

        for item in undeleted_items:
            # special case of file that sometimes created automatically right after delete,
            # and then we can't delete the folder because it's not empty
            if item != self.APPSTORED_PATH:
                raise AfcException(f'failed to clear crash reports directory, undeleted items: {undeleted_items}', None)

    def ls(self, path: str = '/', depth: int = 1) -> List[str]:
        """
        List file and folder in the crash report's directory.
        :param path: Path to list, relative to the crash report's directory.
        :param depth: Listing depth, -1 to list infinite.
        :return: List of files listed.
        """
        return list(self.afc.dirlist(path, depth))[1:]  # skip the root path '/'

    def pull(self, out: str, entry: str = '/', erase: bool = False) -> None:
        """
        Pull crash reports from the device.
        :param out: Directory to pull crash reports to.
        :param entry: File or Folder to pull.
        :param erase: Whether to erase the original file from the CrashReports directory.
        """

        def log(src, dst):
            self.logger.info(f'{src} --> {dst}')

        self.afc.pull(entry, out, callback=log)

        if erase:
            if posixpath.normpath(entry) in ('.', '/'):
                self.clear()
            else:
                self.afc.rm(entry, force=True)

    def flush(self) -> None:
        """ Trigger com.apple.crashreportmover to flush all products into CrashReports directory """
        ack = b'ping\x00'
        assert ack == self.lockdown.start_lockdown_service(self.crash_mover_service_name).recvall(len(ack))

    def watch(self, name: str = None, raw: bool = False) -> Generator[str, None, None]:
        """
        Monitor creation of new crash reports for a given process name.

        Return value can either be the raw crash string, or parsed result containing a more human-friendly
        representation for the crash.
        """
        for syslog_entry in OsTraceService(lockdown=self.lockdown).syslog():
            if (posixpath.basename(syslog_entry.filename) != 'osanalyticshelper') or \
                    (posixpath.basename(syslog_entry.image_name) != 'OSAnalytics') or \
                    not syslog_entry.message.startswith('Saved type '):
                # skip non-ips creation syslog lines
                continue

            filename = posixpath.basename(syslog_entry.message.split()[-1])
            self.logger.debug(f'crash report: {filename}')

            if posixpath.splitext(filename)[-1] not in ('.ips', '.panic'):
                continue

            crash_report_raw = self.afc.get_file_contents(filename).decode()
            crash_report = get_crash_report_from_buf(crash_report_raw, filename=filename)

            if name is None or crash_report.name == name:
                if raw:
                    yield crash_report_raw
                else:
                    yield crash_report

    def get_new_sysdiagnose(self, out: str, erase: bool = True) -> None:
        """
        Monitor the creation of a newly created sysdiagnose archive and pull it
        :param out: filename
        :param erase: remove after pulling
        """
        sysdiagnose_filename = None

        for syslog_entry in OsTraceService(lockdown=self.lockdown).syslog():
            if (posixpath.basename(syslog_entry.filename) not in SYSDIAGNOSE_PROCESS_NAMES) or \
                    (posixpath.basename(syslog_entry.image_name) not in SYSDIAGNOSE_PROCESS_NAMES):
                # filter only sysdianose lines
                continue

            message = syslog_entry.message

            if message.startswith('SDArchive: Successfully created tar at '):
                self.logger.info('sysdiagnose creation has begun')
                for filename in self.ls('DiagnosticLogs/sysdiagnose'):
                    # search for an IN_PROGRESS archive
                    if 'IN_PROGRESS_' in filename:
                        for ext in self.IN_PROGRESS_SYSDIAGNOSE_EXTENSIONS:
                            if filename.endswith(ext):
                                sysdiagnose_filename = filename.rsplit(ext)[0]
                                sysdiagnose_filename = sysdiagnose_filename.replace('IN_PROGRESS_', '')
                                sysdiagnose_filename = f'{sysdiagnose_filename}.tar.gz'
                                break
                break

        self.afc.wait_exists(sysdiagnose_filename)
        time.sleep(IOS17_SYSDIAGNOSE_DELAY)
        self.pull(out, entry=sysdiagnose_filename, erase=erase)


parse_parser = Cmd2ArgumentParser(description='parse given crash report file')
parse_parser.add_argument('filename')

clear_parser = Cmd2ArgumentParser(description='remove all crash reports')


class CrashReportsShell(AfcShell):
    def __init__(self, lockdown: LockdownServiceProvider):
        self.manager = CrashReportsManager(lockdown)
        super().__init__(lockdown, service_name=self.manager.copy_mobile_service_name)
        self.complete_parse = self._complete_first_arg

    @with_argparser(parse_parser)
    def do_parse(self, args) -> None:
        self.poutput(
            get_crash_report_from_buf(self.afc.get_file_contents(args.filename).decode(), filename=args.filename))

    @with_argparser(clear_parser)
    def do_clear(self, args) -> None:
        self.manager.clear()
