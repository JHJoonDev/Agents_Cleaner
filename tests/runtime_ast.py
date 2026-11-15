"""Erase annotation-only syntax; preserve executable AST for type-only QA."""
import ast
import hashlib

class RuntimeOnly(ast.NodeTransformer):
    def visit_If(self, node):
        if isinstance(node.test, ast.Name) and node.test.id == 'TYPE_CHECKING':
            return None
        return self.generic_visit(node)
    def visit_arg(self, node):
        node.annotation = None
        node.type_comment = None
        return node
    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        node.returns = None
        node.type_comment = None
        return node
    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_AnnAssign(self, node):
        if node.value is None:
            return None
        return ast.Assign(targets=[node.target], value=self.visit(node.value))
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == 'cast' and len(node.args) == 2:
            return self.visit(node.args[1])
        return self.generic_visit(node)
    def visit_Assign(self, node):
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id in {'TypeVar','ParamSpec'}:
            return None
        node.type_comment = None
        return self.generic_visit(node)
    def visit_Name(self, node):
        if node.id == 'TkApp': node.id = 'App'
        return node
    def visit_Import(self, node):
        for name in node.names:
            if name.asname == name.name:
                name.asname = None
        return node
    def visit_ImportFrom(self, node):
        if node.module in {'typing','__future__'}:
            return None
        for name in node.names:
            if name.asname == 'TkApp': name.asname = None
        return node
    def visit_ClassDef(self, node):
        self.generic_visit(node)
        node.bases = [base.value if isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name) and base.value.id == 'frozenset' else base for base in node.bases]
        return node

def executable_hash(source):
    tree = RuntimeOnly().visit(ast.parse(source))
    # Additional Entry imports serve deferred annotations only. Original imports
    # used by constructors remain represented in the executable tree.
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    class Imports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            node.names = [n for n in node.names if n.name != 'Entry' or (n.asname or n.name) in used]
            return node if node.names else None
    tree = Imports().visit(tree)
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
