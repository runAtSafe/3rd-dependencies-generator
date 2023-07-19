import argparse
import os
import re
import sys
import time
import threading
import uuid
import json
import pymysql.cursors
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree
from graphviz import Digraph
from build_log import BuildLogParser
from dotenv import load_dotenv

load_dotenv('.env')

class DepGraphBuilder:
    BUILD_LOG_DIR = 'build'
    DEFAULT_FILENAME = 'dep_graph'
    SLN_REGEX = re.compile(r'\.\.\\.*\.vcxproj')
    XML_NAMESPACE = 'http://schemas.microsoft.com/developer/msbuild/2003'
    XML_NAMESPACE_PREFIX = '{' + XML_NAMESPACE + '}'
    DEFAULT_DISPLAY_STRING_TEMPLATE = (
        '{display_name}\n{build_info}'
    )
    MULTI_GRAPH_DISPLAY_STRING_TEMPLATE = (
        '<<table border="0">'
        '<tr><td tooltip="{tooltip}" href="{uuid}.svg">{display_name}</td></tr>'
        '<tr><td tooltip="View Build Info" {build_link}>{build_info}</td></tr>'
        '</table>>'
    )
    WEB_DISPLAY_STRING_TEMPLATE = (
        '<<table border="0">'
        '<tr><td tooltip="{tooltip}" href="{uuid}.svg">{display_name}</td></tr>'
        '<tr><td tooltip="View Build Info" {build_link}>{build_info}</td></tr>'
        '<tr><td tooltip="Hide Node" href="{filename}.svg?hidden={hidden}"><I>[hide]</I></td></tr>'
        '</table>>'
    )

    class PathError(Exception):
        pass

    class RootProjectError(Exception):
        pass

    @staticmethod
    def get_projects_from_solution(sln_path):
        # Open the solution file and collect all
        # the projects within it.
        projects = set()
        sln_dir, _ = os.path.split(sln_path)
        with open(sln_path) as slnFile:
            for line in slnFile:
                if line == 'Global\n':
                    # We've reached the end of project definitions
                    # within the solution file
                    break

                match = DepGraphBuilder.SLN_REGEX.search(line)
                if match:
                    # Replace the relative project path from the solution
                    # with an absolute path using the path to the solution
                    # that the user provided.
                    projects.add(os.path.normpath(
                        os.path.join(sln_dir, match.group(0))).lower())
        return projects

    @staticmethod
    def get_output_files(project_root):
        IGNORED_MACROS = [
            '$(OutDir)', '$(IntermediateOutputPath)', '$(ProjectDir)',
            '$(TargetExt)', '$(IntDir)', '$(TargetDir)', '$(TargetPath)']
        EVAL_MACROS = [
            '$(TargetName)', '$(ProjectName)',
            '$(TargetFileName)', '$(RootNamespace)']

        def get_element_value(element_name):
            element = project_root.find('.//{}{}'.format(
                DepGraphBuilder.XML_NAMESPACE_PREFIX, element_name))
            if element is not None:
                return element.text
            return ''

        output_files = set()
        elements = project_root.findall(
            './/{}OutputFile'.format(DepGraphBuilder.XML_NAMESPACE_PREFIX))
        elements += project_root.findall(
            './/{}ImportLibrary'.format(DepGraphBuilder.XML_NAMESPACE_PREFIX))

        for element in elements:
            for macro in IGNORED_MACROS:
                element.text = element.text.replace(macro, '')
            for macro in EVAL_MACROS:
                if macro in element.text:
                    element.text = element.text.replace(
                        macro, get_element_value(macro[2:-1]))

            _, output_file = os.path.split(element.text)

            output_files.add(output_file)
        return list(output_files)

    @staticmethod
    def get_display_name(project):
        _, name = os.path.split(project)
        if name.endswith('.csproj') or name.endswith('.vbproj'):
            return name
        elif name.endswith('.sln'):
            return name[:-4]
        elif name.endswith('.slnf'):
            return name[:-5]
        else:
            return name[:-8]

    render_lock = threading.Lock()

    def __init__(self, path, out_dir, multi_graph=False, hide_external=None,
                 web_mode=False):
        self.all_projects = set()

        self._build_num_map = {}
        self._build_time_map = {}
        self._external_projects = set()
        self._full_ref_map = {}
        self._full_reverse_ref_map = defaultdict(set)
        self._hide_external_projects = hide_external
        self._max_build_time = 0
        self._min_ref_map = {}
        self._multi_graph = multi_graph
        self._next_build_id = 0
        self._out_dir = out_dir
        self._project_output_map = {}
        self._projects = set()
        self._ref_map = {}
        self._reverse_ref_map = defaultdict(set)
        self._reverse_uuid_map = {}
        self._unused_libs_map = {}
        self._uuid_map = {}
        self._web_mode = web_mode

        if not os.path.exists(self._out_dir):
            os.mkdir(self._out_dir)

        # Check if we have were given a path to a solution file
        # or directory to parse projects from.
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise DepGraphBuilder.PathError(f"Path not found: {path}")

        if path.endswith('.sln') or path.endswith('.slnf'):
            self._path, self._sln_name = os.path.split(path)
        else:
            self._path = os.path.normpath(path).lower()
            self._sln_name = ''
        self._path += '\\'

    def run_data_collection(self):
        print(f'Collecting project paths from {self._sln_name or self._path}')
        if self._sln_name:
            self._projects = self.get_projects_from_solution(
                self._path + self._sln_name)
        else:
            self._projects = set(
                os.path.normpath(str(path)).lower() for path in
                Path(self._path).rglob('*.vcxproj'))

        # Parse references for each project
        for i, project in enumerate(self._projects):
            sys.stdout.write(
                f'Parsing project {i + 1} of {len(self._projects)}  \r')
            refs = self._parse_refs(project)
            self._ref_map[project] = refs
            self._external_projects |= set(refs) - self._projects
        print()

        self.all_projects = self._projects | self._external_projects

        # Try to parse refs from each external project,
        # ignoring files that doesn't exist.
        for i, project in enumerate(self._external_projects):
            self._ref_map[project] = list(
                set(self._parse_refs(project)) & self.all_projects)

        print('Building reference maps...')
        for project in self.all_projects:
            if project not in self._full_ref_map:
                self._build_full_ref_map(project)

        self._build_min_ref_map()
        self._build_reverse_ref_maps()
        self._generate_uuids()

    def parse_build_log(self, log_file=None, parser=None):
        if parser is None and (log_file is None or not os.path.exists(log_file)):
            if log_file is not None:
                print(f'Could not find build log file: {log_file}')
            return
        elif parser is not None:
            log_parser = parser
        else:
            print('Parsing build log...')
            log_parser = BuildLogParser(log_file)
            log_parser.run()

        for build_num, (project, build_ms) in log_parser.build_map.items():
            project = os.path.normpath(project).lower()
            self._build_time_map[project] = build_ms
            self._max_build_time = max(self._max_build_time, build_ms)
            self._build_num_map[project] = build_num

            unused_lib_info = log_parser.unused_libs_map[build_num]
            if len(unused_lib_info) > 0:
                self._unused_libs_map[project] = unused_lib_info

        if self._multi_graph and not parser:
            print('Generating build performance summaries...')
            log_parser.write_build_summary_files(os.path.join(
                self._out_dir, self.BUILD_LOG_DIR))

    def render_graph(self, root_project=None, root_uuid=None, direction='',
                     hidden_list=None):
        if root_project or root_uuid:
            root_project = self.validate_root_project(root_project, root_uuid)
            graph_title = f'<<b>{self.get_display_name(root_project)} Build Dependency Graph</b>>'
            filename = self._uuid_map[root_project]
        elif self._sln_name:
            graph_title = f'<<b>{self._sln_name} Build Dependency Graph</b>>'
            filename = self.get_display_name(self._sln_name)
        else:
            graph_title = '<<b>Build Dependency Graph</b>>'
            filename = self.DEFAULT_FILENAME

        graph_attrs = {
            'concentrate': 'true',
            'label': graph_title,
            'labeljust': 'l',
            'labelloc': 't',
            'nodesep': '1',
            'ranksep': '1 equally',
            'splines': 'ortho'
        }
        root_graph = Digraph(node_attr={'shape': 'box'},
                             graph_attr=graph_attrs)

        if self._multi_graph and root_project:
            self._add_navigation_nodes(root_graph, filename, direction)
            filename += direction

        # Handle any hidden projects
        if self._web_mode and hidden_list:
            hidden_projects = set(
                self._reverse_uuid_map[proj] for proj in hidden_list)
        else:
            hidden_projects = set()

        display_projects = self._get_display_projects(root_project, direction)

        # Add graph nodes
        for project in sorted(display_projects):
            display_string = self._get_node_display_string(
                project, filename, hidden_list)
            if project in hidden_projects:
                root_graph.node(project.replace(':', ''), display_string, style='invis')
            else:
                root_graph.node(
                    project.replace(':', ''), display_string, style='filled',
                    fillcolor=self._get_node_color(project),
                    tooltip=os.path.relpath(
                        project, self._path).replace('\\', '/'))

        self._add_graph_edges(root_graph, display_projects, hidden_projects)

        if self._web_mode and hidden_list:
            filename = str(uuid.uuid1())
        filepath = os.path.join(self._out_dir, filename)

        # The graphviz rendering process is not thread safe
        # so we need a lock here
        self.render_lock.acquire()
        try:
            print(f'Rendering {graph_title[4:-5]}')
            root_graph.render(
                filepath, format='svg',
                view=not((self._multi_graph and root_project) or self._web_mode))
        finally:
            self.render_lock.release()
        return filepath + '.svg'

    def create_build_time_leaderboard(self, num_projects=10):
        leaderboard_projects = [project for project, _ in sorted(
            self._build_time_map.items(), key=lambda item: item[1], reverse=True) if project in self._projects]
        return self._render_leaderboards(
            [(None, leaderboard_projects[:num_projects])], 'build_time_leaderboard', 'Longest Build Time')

    def create_reference_leaderboard(self, num_projects=10):
        leaderboard_projects = [project for project, _ in sorted(
            self._reverse_ref_map.items(), key=lambda item: len(item[1]), reverse=True) if project in self._projects]
        return self._render_leaderboards(
            [(None, leaderboard_projects[:num_projects])], 'reference_leaderboard', 'Most References')

    def create_leaderboards(self, num_projects=10):
        ref_leaderboard_projects = [project for project, _ in sorted(
            self._reverse_ref_map.items(), key=lambda item: len(item[1]), reverse=True) if project in self._projects]
        leaderboards = [('Most References', ref_leaderboard_projects[:num_projects])]

        if self._build_time_map:
            build_leaderboard_projects = [project for project, _ in sorted(
                self._build_time_map.items(), key=lambda item: item[1], reverse=True) if project in self._projects]
            leaderboards.append(('Longest Build Time', build_leaderboard_projects[:num_projects]))

        if self._sln_name:
            graph_title = f'{self._sln_name} Leaderboards'
        else:
            graph_title = None

        return self._render_leaderboards(leaderboards, 'leaderboards', graph_title)

    def validate_root_project(self, root_project, root_uuid):
        if root_project and root_uuid:
            raise DepGraphBuilder.RootProjectError(
                'Only specify one of root_project or root_uuid, not both.')

        if root_uuid and (root_uuid in self._reverse_uuid_map):
            return self._reverse_uuid_map[root_uuid]
        elif root_uuid:
            raise DepGraphBuilder.RootProjectError(
                f'No projects found with uuid "{root_uuid}"')

        matches = [proj for proj in self.all_projects if root_project in proj]
        if len(matches) == 0:
            raise DepGraphBuilder.RootProjectError(
                f'No projects found matching root project name "{root_project}"')
        elif len(matches) > 1:
            raise DepGraphBuilder.RootProjectError(
                f'Root project name "{root_project}" is ambiguous, '
                f'found multiple matches: {matches}')
        return matches[0]

    def _render_leaderboards(self, leaderboards, filename, title=None):

        graph_attrs = {
            'concentrate': 'true',
            'labelloc': 't',
            'nodesep': '1',
            'ranksep': '0.25 equally',
            'splines': 'ortho'
        }
        if title:
            graph_attrs['label'] = f'<<b><u>{title}</u></b>>'
        root_graph = Digraph(node_attr={'shape': 'box'},
                             graph_attr=graph_attrs)

        for i, (header, project_list) in enumerate(leaderboards):
            if header:
                root_graph.node(header, f'<<b>{header}</b>>')
                previous_node = header
            else:
                previous_node = None

            for project in project_list:
                node_name = project.replace(':', '') + str(i)
                display_string = self._get_node_display_string(
                    project, filename)
                root_graph.node(
                    node_name, display_string, style='filled',
                    fillcolor=self._get_node_color(project),
                    tooltip=os.path.relpath(project, self._path).replace('\\', '/'))
                if previous_node:
                    root_graph.edge(previous_node, node_name, style='invis')
                previous_node = node_name

        filepath = os.path.join(self._out_dir, filename)
        self.render_lock.acquire()
        try:
            print(f'Rendering leaderboard...')
            root_graph.render(filepath, format='svg', view=not self._web_mode)
        finally:
            self.render_lock.release()
        return filepath + '.svg'

    def _get_display_projects(self, root_project=None, direction=None):
        if self._hide_external_projects:
            display_projects = self._projects.copy()
        else:
            display_projects = self.all_projects.copy()
        if root_project:
            if direction == 'up':
                # Restrict the display projects to only what the given
                # root project depends on
                display_projects &= self._full_ref_map[root_project]
            elif direction == 'down':
                # Restrict the display projects to only what the given
                # root project is depended on by
                display_projects &= self._full_reverse_ref_map[root_project]
            else:
                display_projects &= (set(self._min_ref_map[root_project]) |
                                     self._reverse_ref_map[root_project])
                display_projects.add(root_project)

        return display_projects

    def _parse_refs(self, project):
        refs = []
        try:
            # Open the project file and search for project references
            root = ElementTree.parse(project).getroot()
            elements = root.findall(
                './/{}ProjectReference'.format(self.XML_NAMESPACE_PREFIX))
            for element in elements:
                if 'Include' not in element.attrib:
                    continue

                ref = element.attrib['Include']
                if '$(SolutionDir)' in ref:
                    ref = ref.replace('$(SolutionDir)', self._path)
                else:
                    proj_dir, _ = os.path.split(project)
                    ref = os.path.join(proj_dir, ref)
                refs.append(os.path.normpath(ref).lower())

            output_files = self.get_output_files(root)
            if len(output_files) > 0:
                self._project_output_map[project] = output_files
        except FileNotFoundError:
            pass

        return refs

    def _build_full_ref_map(self, project):
        self._full_ref_map[project] = {project}
        for ref in self._ref_map.get(project, []):
            if ref in self._full_ref_map[project]:
                # Already added refs from this project, continue
                continue
            if ref not in self._full_ref_map:
                # Haven't computed all refs for this
                # project yet so do that now
                self._build_full_ref_map(ref)
            self._full_ref_map[project] |= self._full_ref_map[ref]

    def _get_filtered_refs(self, project, ref_map, predicate):
        included_refs = set()
        for ref in ref_map.get(project, []):
            if predicate(ref):
                included_refs.add(ref)
            else:
                included_refs.update(self._get_filtered_refs(ref, ref_map, predicate))
        return included_refs

    def _build_min_ref_map(self):
        for project in self.all_projects:
            if self._hide_external_projects:
                refs = self._get_filtered_refs(project, self._ref_map, lambda r: r in self._projects)
            else:
                refs = self._ref_map[project]

            self._min_ref_map[project] = []
            for ref in refs:
                redundant = False
                for otherRef in refs:
                    if ref == otherRef:
                        continue
                    if ref in self._full_ref_map[otherRef]:
                        redundant = True
                        break
                if not redundant:
                    self._min_ref_map[project].append(ref)

    def _build_reverse_ref_maps(self):
        for project, refs in self._full_ref_map.items():
            self._full_reverse_ref_map[project].add(project)
            for ref in refs:
                self._full_reverse_ref_map[ref].add(project)

        for project, refs in self._min_ref_map.items():
            for ref in refs:
                self._reverse_ref_map[ref].add(project)

    def _generate_uuids(self):
        # Proper uuid's are overkill for our use case, we only need a
        # shorthand way to reference a specific project so integer id's
        # will work fine.
        for project in sorted(self.all_projects):
            self._uuid_map[project] = str(self._next_build_id)
            self._reverse_uuid_map[str(self._next_build_id)] = project
            self._next_build_id += 1

    def _add_navigation_nodes(self, root_graph, filename, direction):
        if self._sln_name:
            node_display = self.get_display_name(self._sln_name)
        else:
            node_display = self.DEFAULT_FILENAME
        root_graph.node('Back', label=f'<<I>Back to {node_display}</I>>',
                        URL=f'{node_display}.svg')

        # Add link nodes to show/hide dependencies and references
        if direction == 'up':
            root_graph.node('Hide dependencies',
                            label=f'<<I>Hide dependencies</I>>',
                            URL=f'{filename}.svg')
            root_graph.edge('Back', 'Hide dependencies', style='invis')
        elif direction == 'down':
            root_graph.node('Hide references',
                            label=f'<<I>Hide references</I>>',
                            URL=f'{filename}.svg')
            root_graph.edge('Back', 'Hide references', style='invis')
        else:
            root_graph.node('Show dependencies',
                            label=f'<<I>Show dependencies</I>>',
                            URL=f'{filename}up.svg')
            root_graph.edge('Back', 'Show dependencies', style='invis')
            root_graph.node('Show references',
                            label=f'<<I>Show references</I>>',
                            URL=f'{filename}down.svg')
            root_graph.edge(
                'Show dependencies', 'Show references', style='invis')

    def _get_node_display_string(self, project, filename, hidden_list=None):
        display_name = self.get_display_name(project)

        if project in self._build_time_map:
            build_num = self._build_num_map[project]
            build_time_display = time.strftime(
                '%M:%S', time.gmtime(self._build_time_map[project] / 1000))
            build_info = f'(#{build_num}, {build_time_display})'
            build_link = f'href="/{self.BUILD_LOG_DIR}/build_{build_num}.html"'
        else:
            build_info = build_link = ''

        if self._web_mode:
            template = self.WEB_DISPLAY_STRING_TEMPLATE
        elif self._multi_graph:
            template = self.MULTI_GRAPH_DISPLAY_STRING_TEMPLATE
        else:
            template = self.DEFAULT_DISPLAY_STRING_TEMPLATE

        return template.format(
            tooltip=os.path.relpath(project, self._path),
            filename=filename,
            hidden=sorted((hidden_list or []) + [self._uuid_map[project]]),
            uuid=self._uuid_map[project],
            display_name=display_name,
            build_info=build_info,
            build_link=build_link
        )

    def _get_node_color(self, project):
        if project in self._build_time_map:
            percentage = 1 - (self._build_time_map[project] / self._max_build_time)
            color_value = int(percentage * 0xff)
            return '#ff{:02x}00'.format(color_value)
        elif project in self._external_projects:
            return '#b0b0b0'
        else:
            return '#ffffff'

    def _add_graph_edges(self, root_graph, display_projects, hidden_projects):
        for project in sorted(display_projects):
            for ref in sorted(self._min_ref_map.get(project, [])):
                if ref not in display_projects:
                    continue
                if project in hidden_projects or ref in hidden_projects:
                    root_graph.edge(ref.replace(':', ''), project.replace(':', ''),
                                    style='invis')
                    continue

                unused = False
                if ref in self._project_output_map and project in self._unused_libs_map:
                    for output_file in self._project_output_map[ref]:
                        if '\\' + output_file in self._unused_libs_map[project]:
                            unused = True

                tooltip = f'{self.get_display_name(ref)} -> {self.get_display_name(project)}'
                color = '#ff0000' if unused else '#000000'
                root_graph.edge(ref.replace(':', ''), project.replace(':', ''),
                                tooltip=tooltip, color=color)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('path',
                        help='A path to a .sln file or a directory '
                             'to collect project files from.')
    parser.add_argument('--internal', '-i', action='store_true', default=False,
                        help='Internal projects only. '
                             'Exclude projects that are not contained within the given solution or directory.')
    parser.add_argument('--log', '-l', type=str, default='',
                        help='Path to a build log text file to parse build '
                             'information from. If included projects will '
                             'display build times and be ranked on the graph '
                             'according to build order. Build log must contain '
                             'build timing information (enabled in VS settings) '
                             'and be in real-time (non-build order) order.')
    parser.add_argument('--out', '-o', type=str, default='./dep_graph',
                        help='Output directory.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--multi', '-m', action='store_true', default=False,
                       help='Generate a multi-graph where nodes link '
                            'to their own separate dependency graph')
    group.add_argument('--root', '-r', type=str,
                       help='The name of the project to use as the root of the'
                            ' dependency graph. If not provided all projects'
                            ' in the given solution or directory will be rendered.')
    parser.add_argument('--direction', '-d', type=str, default='',
                        help='Only used when --root (-r) is specified. Must be "up" or "down". Specifies the direction '
                             'from the root project to render dependency graph for. "up" for projects depended on by '
                             'the root, "down" for projects dependent on the root. If not specified, the first layer '
                             'of both directions will be rendered.')
    parsed_args = parser.parse_args()

    try:
        graph_builder = DepGraphBuilder(parsed_args.path, parsed_args.out,
                                        parsed_args.multi, parsed_args.internal)

        graph_builder.run_data_collection()
        full_ref_map = {k: list(v) for k, v in graph_builder._full_ref_map.items()}
        
        component_name_of_interest = parsed_args.root
        print(f"args root: {component_name_of_interest}")

        # search with an optinal '3rd_' in front of the name
        possible_names = [f'\\{component_name_of_interest}.vcxproj',
                  f'\\3rd_{component_name_of_interest}.vcxproj']


        component_of_interest = next((key for key in full_ref_map.keys() if any(key.endswith(name) for name in possible_names)), None)
        print(f"found key: {component_of_interest}")
        print('\n')

        if component_of_interest is not None:
            component_downstream_dependencies = full_ref_map.get(component_of_interest, [])
            # print('\n'.join(component_downstream_dependencies))

            dependency_names = [path.split('\\')[-1].replace('.vcxproj', '') for path in component_downstream_dependencies if path.split('\\')[-1].replace('.vcxproj', '') != component_name_of_interest]
            print('\n'.join(dependency_names))

            connection = pymysql.connect(
                host=os.environ.get('DB_HOST'),
                user=os.environ.get('DB_USER'),
                password=os.environ.get('DB_PASS'),
                database=os.environ.get('DB_NAME'),
                port=int(os.environ.get('DB_PORT'))
            )

            # batch insert to save time and resources
            values = [(component_name_of_interest, dependency_name) for dependency_name in dependency_names ]
            with connection.cursor() as cursor:
                sql = "INSERT INTO third_party.component_dependency (component, dependency) VALUES (%s, %s)"
                cursor.executemany(sql, values)

                connection.commit()

            connection.close()

    except (DepGraphBuilder.PathError, DepGraphBuilder.RootProjectError) as e:
        print(e)
        exit(1)
