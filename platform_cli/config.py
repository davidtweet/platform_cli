#!/usr/bin/env python
# Copyright (C) 2013 Jive Software. All rights reserved.

"""Manage the configuration for a server platform.

Manage a set of variables shared among multiple services that may be used
during startup. Variables have three different incarnations:

  Default: Has a name and a value. Passed into ConfigCLI when it is
  instantiated, based on statically-set values.

  Override: Has a name and a value. Read by ConfigCLI from the global
  user override configuration file.

  Suggestion: Has a name, a value, and a "why" string. Meant to be generated by
  the script that imports ConfigCLI, and passed in when ConfigCLIU is
  instantiated.

Variable values for defaults and overrides can include
{{template.substitutions}} using mustache template syntax.

What doesn't go here:

  * Startup variables that must have their values generated at runtime, such as
    a string with an embedded timestamp. These need to be hard-coded into the
    startup process itself.
"""

import difflib
import sys
import collections
import textwrap
import subprocess

from . import props, protected_file_path, template
from clint.textui import colored, puts, indent


class Error(Exception):
  """Base exception class for this module."""

# pylint: disable=invalid-name
Default = collections.namedtuple('Default', ['name', 'value'])
Override = collections.namedtuple('Override', ['name', 'value'])
Suggestion = collections.namedtuple('Suggestion', ['name', 'value', 'why'])
Doc = collections.namedtuple('Doc', ['name', 'doc'])


def validate_and_map_by_name(variables):
  """Take an iterable of named tuples and map them by 'name' attribute'.

  Disallow duplicate names, spaces and triple-underscore.
  """
  vars_by_name = {}
  for var in variables:
    if '___' in var.name:
      raise Error('Invalid variable name "{}": contains "___".'.format(
                  var.name))
    if ' ' in var.name:
      raise Error('Invalid variable name "{}": contains " ".'.format(
                  var.name))
    if var.name in vars_by_name:
      raise Error('Duplicate variable name.\n'
                  '{} duplicates {}.'.format(var, vars_by_name[var.name]))
    vars_by_name[var.name] = var
  return vars_by_name


class Config(object):
  """Provides an interface to manage a central configuration file."""

  def __init__(self, config_path,
               defaults=None, suggestions=None, docs=None):

    self.config_path = config_path

    self.defaults = defaults if defaults is not None else []
    self.suggestions = suggestions if suggestions is not None else []
    self.docs = docs if docs is not None else []

  def list_vars(self, args):
    """Console output of active variable values."""
    vals, _, different_defaults = self.get_active_values_and_metadata()
    if args.verbose:
      namelist = sorted(vals.keys())
    else:
      namelist = sorted(different_defaults.keys())
    if args.substring_match is not None:
      namelist = [name for name in namelist if args.substring_match in name]
    if namelist:
      column_width = max(len(name) for name in namelist) + 1
      for name in namelist:
        if args.as_props:
          puts('{}={}'.format(name, vals[name]))
        else:
          if name in different_defaults:
            puts(colored.green('{} {} (default is {})'.format(
                 name.ljust(column_width), vals[name], different_defaults[name].value)))
          else:
            puts('{} {}'.format(name.ljust(column_width), vals[name]))

  def enable(self, args):
    """Enable a service."""
    self.set_override('{}.enabled'.format(args.service_name), 'True')

  def disable(self, args):
    """Disable a service."""
    self.delete_override('{}.enabled'.format(args.service_name))

  def set_var(self, args):
    """Set variable override value."""

    self.exit_on_unknown_key(
        args.property_name,
        ('Error: Can\'t set override value for "{}" '
         'because it is an unknown variable name.').format(args.property_name)
    )
    self.set_override(args.property_name, args.property_value)

  def delete_var(self, args):
    """Delete variable override value."""
    self.exit_on_unknown_key(
        args.property_name,
        ('Error: Can\'t delete override value for "{}" '
         'because it is an unknown variable name.').format(args.property_name)
    )
    self.delete_override(args.property_name)

  def exit_on_unknown_key(self, key, message):
    """If a key is not in the defaults, show close matches and exit."""
    default_names = [default.name for default in self.defaults]
    if key not in default_names:
      puts(message)
      close_matches = difflib.get_close_matches(key, default_names)
      if close_matches:
        puts('Did you mean:')
        with indent(4):
          for match in close_matches:
            puts(match)
        puts('?')
      sys.exit(1)

  def get_overrides(self):
    """Get a list of tuples in the override config."""
    with protected_file_path.ProtectedFilePath(self.config_path):
      conf_items = props.get_items(self.config_path, create_new=True)
    return [Override(name, value) for name, value in conf_items]

  def delete_override(self, key):
    """Delete an override for a single key."""
    with protected_file_path.ProtectedFilePath(self.config_path):
      props.delete_key(self.config_path, key, create_new=True)

  def set_override(self, key, value):
    """Set an override for a single key."""
    with protected_file_path.ProtectedFilePath(self.config_path):
      props.set_key(self.config_path, key, value, create_new=True)

  def get_active_values_and_metadata(self):
    """Obtain the active variable mapping plus metadata."""

    defaults_by_name = validate_and_map_by_name(self.defaults)
    overrides_by_name = validate_and_map_by_name(self.get_overrides())
    suggestions_by_name = validate_and_map_by_name(self.suggestions)

    active_values = {}
    different_suggestions = {}
    different_defaults = {}

    for name, default in defaults_by_name.iteritems():
      override = overrides_by_name.get(name)
      if override is not None and override.value != default.value:
        different_defaults[name] = default
        active_values[name] = override.value
      else:
        active_values[name] = default.value
    active_values = template.render_values_in_template_map(active_values)
    for name, value in active_values.iteritems():
      suggestion = suggestions_by_name.get(name)
      if suggestion is not None and suggestion.value != value:
        different_suggestions[name] = suggestion
    return active_values, different_suggestions, different_defaults

  def show_docs(self, _):
    """Show documentation and defaults for variables."""
    output = []
    defaults_by_name = validate_and_map_by_name(self.defaults)
    docs_by_name = validate_and_map_by_name(self.docs)
    names_by_category = {}
    for name in docs_by_name:
      category = name.split('.')[0]
      names_by_category.setdefault(category, [])
      names_by_category[category].append(name)
    categories = sorted(names_by_category.keys())
    categories.remove('main')
    categories = ['main'] + categories
    for category in categories:
      output.append(category.title())
      output.append('-' * len(category))
      for name in sorted(names_by_category[category]):
        output.append('    {}'.format(name))
        output.extend('        {}'.format(line) for line in textwrap.wrap(docs_by_name[name].doc))
        output.append('            Default: {}\n'.format(defaults_by_name[name].value))
      output.append('\n')
    try:
      proc = subprocess.Popen(['less', '-K', '-'], stdin=subprocess.PIPE)
      proc.communicate('\n'.join(output))
    except KeyboardInterrupt:
      sys.exit(0)
