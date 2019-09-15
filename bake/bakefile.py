import sys
import os
import json
from random import randint

import click

from .bash import Bash

INDENT_STYLES = ("\t", " " * 4)


class NoBakefileFound(RuntimeError):
    pass


class TaskNotInBashfile(ValueError):
    pass


class FilterNotAvailable(ValueError):
    pass


class TaskFilter:
    def __init__(self, s):
        self.source = s

    def __str__(self):
        return f"{self.source!r}"

    @property
    def name(self):
        return self.source.split(":", 1)[0][len("@") :]

    @property
    def arguments(self):
        arguments = {}

        try:

            for arg in self.source.split(":", 1)[1].split(":"):
                split = arg.split("=", 1)

                key = split[0]
                value = split[1] if len(split) == 2 else True

                arguments[key] = value
        except IndexError:
            pass

        return arguments

    def depends_on(self, **kwargs):
        return []

    @staticmethod
    def execute_confirm(*, prompt=False, yes=False, secure=False, **kwargs):
        if not yes:
            if secure:
                int1 = randint(0, 12)
                int2 = randint(0, 12)

                user_value = click.prompt(f"   What is {int1} times {int2}?")

                if int(user_value) != int1 * int2:
                    sys.exit(1)

            else:
                click.confirm("   Do you want to continue?", abort=True)

    def execute(self, yes=False, **kwargs):
        if self.name == "confirm":
            self.execute_confirm(yes=yes, **self.arguments)


class TaskScript:
    def __init__(self, bashfile, chunk_index=None):
        self.bashfile = bashfile
        self._chunk_index = chunk_index

        if self._chunk_index is None:
            raise TaskNotInBashfile()

    def __repr__(self):
        return f"<TaskScript name={self.name!r} depends_on={self.depends_on(recursive=True)!r}>"

    def __str__(self):
        return f"{self.name!r}"

    @property
    def declaration_line(self):
        for line in self.bashfile.source_lines:
            if line.startswith(self.name):
                return line

    def depends_on(self, *, reverse=False, recursive=False):
        def gen_actions():
            task_strings = self.declaration_line.split(":", 1)[1].split()

            task_name_index_tuples = [
                (self.bashfile.find_chunk(task_name=s), s) for s in task_strings
            ]

            for i, task_string in task_name_index_tuples:

                if i is None:
                    # Create the filter.
                    yield TaskFilter(task_string)
                else:
                    # Otherwise, create the task.
                    yield TaskScript(bashfile=self.bashfile, chunk_index=i)

        actions = [t for t in gen_actions()]

        if recursive:
            for i, task in enumerate(actions[:]):
                for t in reversed(task.depends_on()):
                    if t.name not in [task.name for task in actions]:
                        actions.insert(i + 1, t)

        return actions

    @classmethod
    def _from_chunk_index(Class, bashfile, *, i):

        return Class(bashfile=bashfile, chunk_index=i)

    @staticmethod
    def _transform_line(line, *, indent_styles=INDENT_STYLES):
        for indent_style in indent_styles:
            if line.startswith(indent_style):
                return line[len(indent_style) :]

    def execute(self, *, blocking=False, debug=False, silent=False, **kwargs):
        from tempfile import mkstemp
        import stat
        from shlex import quote as shlex_quote

        tf = mkstemp(suffix=".sh", prefix="bashf-")[1]

        with open(tf, "w") as f:
            f.write(self.source)

        # Mark the temporary file as executable.
        st = os.stat(tf)
        os.chmod(tf, st.st_mode | stat.S_IEXEC)

        stdlib_path = os.path.join(os.path.dirname(__file__), "scripts", "stdlib.sh")

        args = [shlex_quote(a) for a in self.bashfile.args]

        if silent:
            script = shlex_quote(f"{tf} {args}")
        else:
            script = shlex_quote(f"{tf} {args} 2>&1 | bake-indent")
        cmd = f"bash --init-file {shlex_quote(stdlib_path)} -i -c {script}"
        if debug:
            print(cmd)

        return os.system(cmd)

    @property
    def name(self):
        return self.chunk[0].split(":")[0].strip()

    @property
    def chunk(self):
        return self.bashfile.chunks[self._chunk_index]

    def _iter_source(self):
        for line in self.chunk[1:]:
            line = self._transform_line(line)
            if line:
                yield line

    @property
    def source(self):
        return "\n".join([s for s in self._iter_source()])

    @property
    def source_lines(self):
        def gen():
            for line in self.bashfile.source_lines:
                pass


class Bakefile:
    def __init__(self, *, path):
        self.path = path
        self.environ = os.environ
        self._chunks = []
        self.args = []

        if not os.path.exists(path):
            raise NoBakefileFound()

        self.chunks

    def __repr__(self):
        return f"<Bakefile path={self.path!r}>"

    def __getitem__(self, key):
        return self.tasks[key]

    def _iter_chunks(self):
        task_lines = [tl for tl in self._iter_task_lines()]

        for i, (index, declaration_line) in enumerate(task_lines):
            try:
                end_index = task_lines[i + 1][0]
            except IndexError:
                end_index = None

            yield self.source_lines[index:end_index]

    def _iter_task_lines(self):
        for i, line in enumerate(self.source_lines):
            if line:
                if self._is_declaration_line(line):
                    yield (i, line.rstrip())

    @property
    def chunks(self):
        if not self._chunks:
            self._chunks = [c for c in self._iter_chunks()]
        return self._chunks

    def find_chunk(self, task_name):
        for i, chunk in enumerate(self.chunks):
            if chunk[0].split(":")[0].strip() == task_name:
                return i

    def __iter__(self):
        return (v for v in self.tasks.values())

    def add_args(self, *args):
        self.args.extend(args)

    def add_environ(self, key, value):
        self.environ[key] = value

    def add_environ_json(self, s):
        try:
            j = json.loads(s)
        except json.JSONDecodeError:
            assert os.path.exists(s)
            # Assume a path was passed, instead.
            with open(s, "r") as f:
                j = json.load(f)

        self.environ.update(j)

    @property
    def home_path(self):
        return os.path.abspath(os.path.dirname(self.path))

    @classmethod
    def find(
        Class, *, filename="Bashfile", root=os.getcwd(), max_depth=4, topdown=True
    ):
        """Returns the path of a Pipfile in parent directories."""
        i = 0
        for c, d, f in os.walk(root, topdown=topdown):
            if i > max_depth:
                raise NoBakefileFound(f"No {filename} found!")
            elif filename in f:
                return Class(path=os.path.join(c, filename))
            i += 1

    @property
    def source(self):
        with open(self.path, "r") as f:
            return f.read()

    @property
    def source_lines(self):
        return self.source.split("\n")

    @staticmethod
    def _is_declaration_line(line):
        return not (line.startswith(" ") or line.startswith("\t"))

    @property
    def tasks(self):
        tasks = {}
        for i, chunk in enumerate(self.chunks):
            script = TaskScript._from_chunk_index(bashfile=self, i=i)
            tasks[script.name] = script

        return tasks
