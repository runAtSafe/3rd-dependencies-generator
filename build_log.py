import argparse
import csv
import os
import re
from collections import defaultdict


class BuildLogParser:
    BUILD_START_REGEX = re.compile(r'^(\d+)>------ Build started:')
    COMPILE_TIME_REGEX = re.compile(r'(\d+)> *(\d+) ms  ClCompile')
    LINK_TIME_REGEX = re.compile(r'(\d+)> *(\d+) ms  Link')
    PERFORMANCE_SUMMARY_REGEX = re.compile(r'(\d+)> *(\d+) ms *(.*\.vcxproj)')
    PERFORMANCE_SUMMARY_FOOTNOTE = '(* = timing was not recorded because of reentrancy)'
    BUILD_HTML_TEMPLATE = (
        '<table><tr>'
        '<td><pre>{build_summary}</pre></td>'
        '<td valign="top"><pre>{divider}</pre></td>'
        '<td valign="top"><pre>{unused_libs}</pre></td>'
        '</tr></table>'
    )

    def __init__(self, file):
        self.build_map = {}
        self.unused_libs_map = defaultdict(str)

        self._build_summary_map = {}
        self._compile_time_map = {}
        self._file = file
        self._link_time_map = {}

    def run(self):
        with open(self._file, 'r') as file:
            found_project_summary = False
            found_target_summary = False
            found_unused_libs = False
            current_build_num = ''
            current_build_info = ''
            for line in file:
                match = self.BUILD_START_REGEX.search(line)
                if match:
                    # A project has started building. Add  it's build
                    # number to the current build timeline position
                    build_num = line[:line.index('>')]
                elif line.endswith('Project Performance Summary:\n'):
                    # A project has finished building and we are now inside
                    # the Project Performance Summary section of the log.
                    found_project_summary = True
                elif line.endswith('Target Performance Summary:\n'):
                    found_target_summary = True
                elif line.endswith('Unused libraries:\n'):
                    found_unused_libs = True
                    current_build_num = line[:line.index('>')]
                elif line.endswith('Unused delay load specifications:\n'):
                    found_unused_libs = True
                    current_build_num = line[:line.index('>')]

                if found_project_summary or found_target_summary or found_unused_libs:
                    current_build_info += line

                if line.endswith(
                        '>\n') or self.PERFORMANCE_SUMMARY_FOOTNOTE in line:
                    if found_project_summary:
                        # We've reached the end of the Project Performance Summary
                        # section. The line before this (last_line) has all
                        # the information we want
                        found_project_summary = False
                        match = self.PERFORMANCE_SUMMARY_REGEX.search(last_line)
                        if match:
                            current_build_num = match.group(1)
                            self.build_map[current_build_num] = (
                                match.group(3), int(match.group(2)))
                    elif found_target_summary:
                        # We've reached the end of the Target Performance Summary section.
                        found_target_summary = False
                        self._build_summary_map[current_build_num] = current_build_info

                        project, _ = self.build_map[current_build_num]
                        match = self.COMPILE_TIME_REGEX.search(current_build_info)
                        if match:
                            self._compile_time_map[project] = match.group(2)

                        match = self.LINK_TIME_REGEX.search(current_build_info)
                        if match:
                            self._link_time_map[project] = match.group(2)

                        current_build_info = ''
                    elif found_unused_libs:
                        found_unused_libs = False
                        self.unused_libs_map[current_build_num] += current_build_info
                        current_build_info = ''
                elif found_project_summary:
                    # Keep saving the current line until we find the end
                    # of the Project Performance Summary section.
                    last_line = line

    def write_build_summary_files(self, out_dir='.'):
        if not os.path.exists(out_dir):
            os.mkdir(out_dir)

        for build_num, build_summary in self._build_summary_map.items():
            with open(f'{out_dir}/build_{build_num}.html', 'w') as file:
                if build_num in self.unused_libs_map:
                    file.write(self.BUILD_HTML_TEMPLATE.format(
                        build_summary=build_summary,
                        divider="\t\t<br>"*self.unused_libs_map[build_num].count('\n'),
                        unused_libs=self.unused_libs_map[build_num]
                    ))
                else:
                    file.write(f'<pre>{build_summary}</pre>')

    def write_build_times(self):
        build_time_dicts = defaultdict(dict)
        for _, (project, build_time) in self.build_map.items():
            build_time_dicts[project]['project'] = project
            build_time_dicts[project]['total_build_time'] = build_time

        for project, compile_time in self._compile_time_map.items():
            build_time_dicts[project]['compile_time'] = compile_time

        for project, link_time in self._link_time_map.items():
            build_time_dicts[project]['link_time'] = link_time

        with open('build_times.csv', 'w') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=['project', 'total_build_time', 'compile_time', 'link_time'])
            writer.writeheader()
            writer.writerows(build_time_dicts.values())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('path',
                        help='The path to the build log to parse. Build log '
                             'must contain build timing information '
                             '(enabled in VS settings) and be in real-time '
                             '(non-build order) order.')
    parsed_args = parser.parse_args()

    parser = BuildLogParser(parsed_args.path)
    parser.run()
    parser.write_build_times()
    print(parser.build_map)
