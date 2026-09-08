"""
Service layer: application use-cases.

Each public method on MemoryService corresponds to one use-case (create
a memory, update a memory, search memories...). Responsibilities here:
open exactly one UnitOfWork per use-case, delegate business rules to
MemoryEngine, call the repository, commit once, and log. No SQL and no
domain validation logic lives in this layer — both belong one layer
down.
"""
