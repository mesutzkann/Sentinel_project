"""Everything that produces the router model: its dataset now, its QLoRA run next.

Separate from `routing/`, which is what the service *uses* at run time. Nothing in the running
service imports from here, and nothing here is installed on the path a request takes — the
training dependencies are heavy and optional, and an import that crossed this line would make
them neither.
"""
