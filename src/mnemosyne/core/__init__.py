"""
Core layer: the "Memory Engine" itself.

This package contains business rules that operate on domain entities —
validation, version-snapshot construction, status-transition rules —
with no I/O of any kind. It sits between the domain layer (which
defines *what things are*) and the service layer (which defines *how
use-cases are orchestrated*, including transactions and persistence).
"""
