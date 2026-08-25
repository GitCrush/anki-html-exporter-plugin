"""Implementation package for the Anki HTML Exporter add-on.

The add-on root ``__init__.py`` only wires up menu entries; everything else
lives here:

``assets``       access to Anki's own reviewer stylesheet / MathJax bundle
``media_export`` copying (or inlining) referenced media files
``renderer``     turning a card into the HTML the reviewer would show
``writer``       assembling the export document(s)
``dialog``       the Qt front end
"""

# Kept in sync with manifest.json by build_addon.py. Shown in the dialog title
# so that which build is running can be told at a glance -- add-ons are only
# imported at start-up, so an edited source file is not necessarily the code
# Anki is executing.
__version__ = "2.3.0"
