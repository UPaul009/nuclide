#!/usr/bin/env python

# Copyright (c) 2015-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree.

# To ensure that Nuclide is easy to set up and install,
# we want to limit our dependencies to built-in Python libraries and libclang
# (which is provided in ../pythonpath)
from clang.cindex import *
from codecomplete import CompletionCache
from ctypes import *
from declarationlocation import get_declaration_location_and_spelling
import outline

import json
import getpass
import hashlib
import logging
import os
import re
import sys
import tempfile
import time
import traceback
from distutils.version import LooseVersion
from logging import FileHandler
from utils import is_header_file, resolve_file, range_dict, location_dict

LOGGING_DIR = 'nuclide-%s-logs/clang' % getpass.getuser()
FD_FOR_READING = 3

# Unfortunately Clang has no way of limiting autocompletion results, but set a reasonable limit
# to avoid overloading the Atom UI.
COMPLETIONS_LIMIT = 100


# Clang warns when you use #pragma once in the main compilation unit.
# However, we often build compilation units from header files here, so avoid the nag.
# https://llvm.org/bugs/show_bug.cgi?id=16686
PRAGMA_ONCE_IN_MAIN_FILE = '#pragma once in main file'


root_logger = logging.getLogger()


def log_filename(value):
    hash = hashlib.md5(value).hexdigest()[:10]
    return os.path.basename(value) + '-' + hash + '.log'


def set_up_logging(src):
    # Be consistent with the main Nuclide logs.
    if sys.platform == 'win32':
        log_dir = os.path.join(tempfile.gettempdir(), LOGGING_DIR)
    else:
        log_dir = os.path.join('/tmp', LOGGING_DIR)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    handler = FileHandler(os.path.join(log_dir, log_filename(src)))
    handler.setFormatter(logging.Formatter(
        'nuclide-clang-py %(asctime)s: [%(name)s] %(message)s'
    ))
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    root_logger.info('starting for ' + src)


def wait_for_init():
    fd = FD_FOR_READING
    mode = 'r'
    buffering = 1  # 1 means line-buffered.
    input_stream = os.fdopen(fd, mode, buffering)
    output_stream = sys.stdout
    first_line = input_stream.readline()
    if first_line.startswith('init:'):
        src = first_line[5:-1]
        # Client is initiating connection. Acknowledge!
        output_stream.write('ack\n')
        output_stream.flush()
        return src, input_stream, output_stream
    else:
        # Fail: did not receive proper initialization sequence.
        sys.exit(2)


def child_diagnostics(lib, diag):
    class ChildDiagnosticsIterator:

        def __init__(self, diag):
            self.ds = lib.clang_getChildDiagnostics(diag)

        def __len__(self):
            return int(lib.clang_getNumDiagnosticsInSet(self.ds))

        def __getitem__(self, key):
            diag = lib.clang_getDiagnosticInSet(self.ds, key)
            if not diag:
                raise IndexError
            return Diagnostic(diag)

    return ChildDiagnosticsIterator(diag)


class Server:
    # Extra functions from the libclang API.
    # TOOD(hansonw): Remove this when these bindings are upstreamed.
    CUSTOM_CLANG_FUNCTIONS = [
        ("clang_getChildDiagnostics",
         [Diagnostic],
            POINTER(c_void_p)),

        ("clang_getNumDiagnosticsInSet",
            [POINTER(c_void_p)],
            c_uint),

        ("clang_getDiagnosticInSet",
            [POINTER(c_void_p), c_uint],
            POINTER(c_void_p)),

        ("clang_getClangVersion",
            [],
            POINTER(c_void_p)),

        ("clang_getCString",
            [c_void_p],
            c_char_p),

        ("clang_sortCodeCompletionResults", [c_void_p, c_uint], None),
    ]

    # New in Clang 3.8: not in the Python bindings yet.
    # Should also be removed once upstreamed.
    PARSE_CREATE_PREAMBLE_ON_FIRST_PARSE = 0x100

    # Prefix of the string returned by clang_getClangVersion.
    CLANG_VERSION_PREFIX = 'clang version'

    def __init__(self, src, input_stream, output_stream):
        self.src = src
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.index = Index.create()
        self.translation_unit = None
        self.completion_cache = None
        self.cached_contents = None
        conf = Config()
        self.custom_clang_lib = conf.lib
        self._register_custom_clang_functions()

        # Cache the libclang version.
        cxstr = self.custom_clang_lib.clang_getClangVersion()
        version = self.custom_clang_lib.clang_getCString(cxstr)

        if version.startswith(Server.CLANG_VERSION_PREFIX):
            version = version[len(Server.CLANG_VERSION_PREFIX):]
        else:
            version = '3.7.0'
        self.clang_version = LooseVersion(version)

    def run(self):
        input_stream = self.input_stream
        output_stream = self.output_stream
        while True:
            line = input_stream.readline()
            response = self.process_request(line)
            json.dump(response, output_stream)
            # Use \n to signal the end of the response.
            output_stream.write('\n')
            output_stream.flush()

    def process_request(self, line):
        '''Note that line will likely including a trailing newline.

        Returns a dict or list that can be serialized by json.dump().
        '''
        request = json.loads(line)

        # Every request should have an id that must also be present in the
        # response.
        reqid = request['reqid']
        response = {'reqid': reqid}

        start_time = time.time()
        try:
            method = request['method']
            if method == 'compile':
                self.compile(request, response)
            elif method == 'get_completions':
                self.get_completions(request, response)
            elif method == 'get_declaration':
                self.get_declaration(request, response)
            elif method == 'get_declaration_info':
                self.get_declaration_info(request, response)
            elif method == 'get_outline':
                self.get_outline(request, response)
            else:
                response[
                    'error'] = 'Unknown method to clang_server.py: %s.' % method
        except:
            response['error'] = traceback.format_exc()

        root_logger.info('Finished %s request in %.2lf seconds.',
                         method, time.time() - start_time)

        # response must have a key named "error" if there was a failure of any
        # kind.
        return response

    def compile(self, request, response):
        contents = request['contents']
        flags = request['flags']

        # Update the translation unit with the latest contents.
        # Force a re-parse, in case the user e.g. changed a header file.
        self.cached_contents = None
        translation_unit = self._update_translation_unit(contents, flags)
        if not translation_unit:
            sys.stderr.write(
                'Suspicious: requesting compilation of %s without flags' % self.src)
            response['diagnostics'] = []
            return

        # Return the diagnostics.
        diagnostics = []
        for diag in translation_unit.diagnostics:
            if diag.spelling == PRAGMA_ONCE_IN_MAIN_FILE and is_header_file(self.src):
                continue
            diagnostics.append(self.diagnostic_dict(diag))
        response['diagnostics'] = diagnostics

    def diagnostic_dict(self, diag):
        ranges = map(range_dict, diag.ranges)
        if len(ranges) == 0:
            ranges = None
        fixits = []
        for fixit in diag.fixits:
            fixits.append({
                'range': range_dict(fixit.range),
                'value': fixit.value,
            })
        children = []
        for child in child_diagnostics(self.custom_clang_lib, diag):
            children.append({
                'spelling': child.spelling,
                'location': location_dict(child.location),
                'ranges': map(range_dict, child.ranges),
            })
            # Some fixits may be nested; add them to the root diagnostic.
            for fixit in child.fixits:
                fixits.append({
                    'range': range_dict(fixit.range),
                    'value': fixit.value,
                })
        return {
            'spelling': diag.spelling,
            'severity': diag.severity,
            'location': location_dict(diag.location),
            'ranges': ranges,
            'fixits': fixits,
            'children': children,
        }

    def get_completions(self, request, response):
        contents = request['contents']
        line = request['line']
        column = request['column']
        prefix = request['prefix']
        token_start_column = request['tokenStartColumn']
        flags = request['flags']

        # NOTE: there is no need to update the translation unit here.
        # libclang's completions API seamlessly takes care of unsaved content
        # without any special handling.
        translation_unit = self._get_translation_unit(None, flags)
        if translation_unit:
            if self.completion_cache is None:
                self.completion_cache = CompletionCache(
                    self.src, translation_unit, self.custom_clang_lib)
            completions = self.completion_cache.get_completions(
                line + 1,
                token_start_column + 1,
                prefix,
                contents,
                limit=COMPLETIONS_LIMIT)
        else:
            completions = []
        response['completions'] = completions
        response['line'] = line
        response['column'] = column
        response['prefix'] = prefix

    def get_declaration(self, request, response):
        contents = request['contents']
        line = request['line']
        column = request['column']
        flags = request['flags']

        response['line'] = line
        response['column'] = column

        # Update the translation unit with the latest contents.
        translation_unit = self._update_translation_unit(contents, flags)
        if not translation_unit:
            return

        response['locationAndSpelling'] = get_declaration_location_and_spelling(
            translation_unit, contents, flags, self.src, line + 1, column + 1)

    def get_declaration_info(self, request, response):
        contents = request['contents']
        line = request['line']
        column = request['column']
        flags = request['flags']

        response['line'] = line
        response['column'] = column

        # Update the translation unit with the latest contents.
        translation_unit = self._update_translation_unit(contents, flags)
        if not translation_unit:
            return

        location = translation_unit.get_location(src, (line + 1, column + 1))
        cursor = Cursor.from_location(translation_unit, location)
        cursor = cursor.referenced
        if cursor is None:
            return

        response['info'] = self.get_declaration_info_for_cursor(cursor)

    def get_declaration_info_for_cursor(self, cursor):
        '''Returns string id in clang-callgraph-service format for entity under the
        cursor. Currently works only for definitions of class methods, instance
        methods and functions. Returns None for everything else.
        '''
        result = []
        while cursor is not None and not cursor.kind.is_translation_unit():
            file = cursor.location.file
            result.append({
                'name': self.get_name_for_cursor(cursor),
                'type': cursor.kind.name,
                'cursor_usr': cursor.get_usr(),
                'file': resolve_file(file),
            })
            cursor = cursor.semantic_parent

        return result

    def get_name_for_cursor(self, cursor):
        name = cursor.displayname
        # clang doesn't include the interface name for categories; add it
        # manually
        if (cursor.kind == CursorKind.OBJC_CATEGORY_DECL or
                cursor.kind == CursorKind.OBJC_CATEGORY_IMPL_DECL):
            # Find reference to base class.
            base_name = ''
            for child in cursor.get_children():
                if child.kind == CursorKind.OBJC_CLASS_REF:
                    base_name = child.displayname
                    break
            return base_name + ' (' + name + ')'
        return name

    def get_outline(self, request, response):
        contents = request['contents']
        flags = request['flags']
        translation_unit = self._update_translation_unit(contents, flags)
        if not translation_unit:
            return
        response['outline'] = outline.get_outline(translation_unit, self.src)

    def _get_translation_unit(self, unsaved_contents, flags=None):
        '''
        Get the current translation unit, or create it if it does not exist.
        Flags can be optional if the translation unit already exists.
        '''
        if self.translation_unit is not None:
            return self.translation_unit

        if flags is None:
            return None

        # Configure the options.
        # See also clang_defaultEditingTranslationUnitOptions in Index.h.
        options = (
            TranslationUnit.PARSE_PRECOMPILED_PREAMBLE |
            TranslationUnit.PARSE_CACHE_COMPLETION_RESULTS |
            TranslationUnit.PARSE_INCLUDE_BRIEF_COMMENTS_IN_CODE_COMPLETION |
            TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD |
            TranslationUnit.PARSE_INCOMPLETE)

        # Clang 3.8 comes with CXTranslationUnit_CreatePreambleOnFirstParse,
        # which allows us to skip the forced reparse.
        # Otherwise, we have have to force an immediate reparse to generate
        # precompiled headers (necessary for fast autocompletion).
        if self.clang_version >= LooseVersion('3.8'):
            options |= Server.PARSE_CREATE_PREAMBLE_ON_FIRST_PARSE
            self.cached_contents = unsaved_contents

        args = self._get_args_for_flags(flags)
        self.translation_unit = self.index.parse(
            self.src, args, self._make_files(unsaved_contents), options)
        return self.translation_unit

    # Clang's API expects a list of (src, contents) pairs.
    def _make_files(self, unsaved_contents):
        if unsaved_contents is None:
            return []
        return [(self.src, unsaved_contents.encode('utf-8'))]

    def _get_args_for_flags(self, flags):
        # Enable typo-detection (and the corresponding fixits)
        # For some reason this is not enabled by default in libclang.
        args = ['-fspell-checking']
        for arg in flags:
            if arg == self.src:
                # Including the input file as an argument causes index.parse() to fail.
                # Surprisingly, including the path to the clang binary
                # as the first argument does not cause any issues.
                pass
            elif arg == '-c':
                # No need to generate a .o file.
                args.append('-fsyntax-only')
            elif arg == '-Werror':
                # We disable this so that the severity can be better reflected in the UI.
                # For example, this allows unused code to appear as a warning
                # instead of an error.
                pass
            elif arg == '-MMD' or arg == '-MD':
                # Do not write out dependency files.
                pass
            else:
                args.append(arg)
        return args

    def _update_translation_unit(self, unsaved_contents=None, flags=None):
        translation_unit = self._get_translation_unit(unsaved_contents, flags)
        if translation_unit is None:
            return None
        # Reparsing isn't cheap, so skip it if nothing changed.
        if (unsaved_contents is not None and
                unsaved_contents == self.cached_contents):
            return translation_unit
        options = 0  # There are no reparse options available in libclang yet.
        translation_unit.reparse(self._make_files(unsaved_contents), options)
        self.cached_contents = unsaved_contents
        if self.completion_cache is not None:
            self.completion_cache.invalidate()
        return translation_unit

    def _register_custom_clang_functions(self):
        # Extend the Clang C bindings with the additional required functions.
        for item in Server.CUSTOM_CLANG_FUNCTIONS:
            func = getattr(self.custom_clang_lib, item[0])
            func.argtypes = item[1]
            func.restype = item[2]


if __name__ == '__main__':
    lib_clang_file = os.environ.get('LIB_CLANG_LIBRARY_FILE')
    if lib_clang_file:
        Config.set_library_file(lib_clang_file)
    src, input_stream, output_stream = wait_for_init()
    set_up_logging(src)
    Server(src, input_stream, output_stream).run()
