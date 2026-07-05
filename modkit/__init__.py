"""modkit - composable PopCap-Lua bytecode transforms for Bookworm Adventures mods.

A mod never ships a finished .luc. It ships *transforms* that the client applies,
in a deterministic order, onto the unpacked files of the user's own game. Because
transforms target protos by content (marker constants) and compute indices
dynamically, independently authored mods compose onto the same file automatically.
"""

__version__ = "0.1.0"
