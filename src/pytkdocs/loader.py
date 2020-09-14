"""
This module is responsible for loading the documentation from Python objects.

It uses [`inspect`](https://docs.python.org/3/library/inspect.html) for introspecting objects,
iterating over their members, etc.
"""

import importlib
import inspect
import pkgutil
import re
from functools import lru_cache
from itertools import chain
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from pytkdocs.objects import Attribute, Class, Function, Method, Module, Object, Source
from pytkdocs.parsers.attributes import get_class_attributes, get_instance_attributes, get_module_attributes, merge
from pytkdocs.parsers.docstrings import PARSERS
from pytkdocs.properties import RE_SPECIAL


class ObjectNode:
    """
    Helper class to represent an object tree.

    It's not really a tree but more a backward-linked list:
    each node has a reference to its parent, but not to its child (for simplicity purposes and to avoid bugs).

    Each node stores an object, its name, and a reference to its parent node.
    """

    def __init__(self, obj: Any, name: str, parent: Optional["ObjectNode"] = None) -> None:
        """
        Initialization method.

        Arguments:
            obj: A Python object.
            name: The object's name.
            parent: The object's parent node.
        """
        try:
            obj = inspect.unwrap(obj)
        except Exception:  # noqa: S110 (we purposely catch every possible exception)
            # inspect.unwrap at some point runs hasattr(obj, "__wrapped__"),
            # which triggers the __getattr__ method of the object, which in
            # turn can raise various exceptions. Probably not just __getattr__.
            # See https://github.com/pawamoy/pytkdocs/issues/45
            pass

        self.obj: Any = obj
        """The actual Python object."""

        self.name: str = name
        """The Python object's name."""

        self.parent: Optional[ObjectNode] = parent
        """The parent node."""

    @property
    def dotted_path(self) -> str:
        """The Python dotted path of the object."""
        parts = [self.name]
        current = self.parent
        while current:
            parts.append(current.name)
            current = current.parent
        return ".".join(reversed(parts))

    @property
    def file_path(self) -> str:
        """The object's module file path."""
        return inspect.getabsfile(self.root.obj)

    @property
    def root(self) -> "ObjectNode":
        """The root of the tree."""
        if self.parent is not None:
            return self.parent.root
        return self

    def is_module(self) -> bool:
        """Is this node's object a module?"""
        return inspect.ismodule(self.obj)

    def is_class(self) -> bool:
        """Is this node's object a class?"""
        return inspect.isclass(self.obj)

    def is_function(self) -> bool:
        """Is this node's object a function?"""
        return inspect.isfunction(self.obj)

    def is_coroutine_function(self) -> bool:
        """Is this node's object a coroutine?"""
        return inspect.iscoroutinefunction(self.obj)

    def is_property(self) -> bool:
        """Is this node's object a property?"""
        return isinstance(self.obj, property)

    def parent_is_class(self) -> bool:
        """Is the object of this node's parent a class?"""
        return bool(self.parent and self.parent.is_class())

    def is_method(self) -> bool:
        """Is this node's object a method?"""
        return self.parent_is_class() and isinstance(self.obj, type(lambda: 0))

    def is_staticmethod(self) -> bool:
        """Is this node's object a staticmethod?"""
        if not self.parent:
            return False
        return self.parent_is_class() and isinstance(self.parent.obj.__dict__.get(self.name, None), staticmethod)

    def is_classmethod(self) -> bool:
        """Is this node's object a classmethod?"""
        if not self.parent:
            return False
        return self.parent_is_class() and isinstance(self.parent.obj.__dict__.get(self.name, None), classmethod)


def get_object_tree(path: str) -> ObjectNode:
    """
    Transform a path into an actual Python object.

    The path can be arbitrary long. You can pass the path to a package,
    a module, a class, a function or a global variable, as deep as you
    want, as long as the deepest module is importable through
    `importlib.import_module` and each object is obtainable through
    the `getattr` method. It is not possible to load local objects.

    Args:
        path: the dot-separated path of the object.

    Raises:
        ValueError: when the path is not valid (evaluates to `False`).
        ImportError: when the object or its parent module could not be imported.

    Returns:
        The leaf node representing the object and its parents.
    """
    if not path:
        raise ValueError(f"path must be a valid Python path, not {path}")

    # We will try to import the longest dotted-path first.
    # If it fails, we remove the right-most part and put it in a list of "objects", used later.
    # We loop until we find the deepest importable submodule.
    obj_parent_modules = path.split(".")
    objects: List[str] = []

    while True:
        parent_module_path = ".".join(obj_parent_modules)
        try:
            parent_module = importlib.import_module(parent_module_path)
        except ImportError:
            if len(obj_parent_modules) == 1:
                raise ImportError("No module named '%s'" % obj_parent_modules[0])
            objects.insert(0, obj_parent_modules.pop(-1))
        else:
            break

    # We now have the module containing the desired object.
    # We will build the object tree by iterating over the previously stored objects names
    # and trying to get them as attributes.
    current_node = ObjectNode(parent_module, parent_module.__name__)
    for obj_name in objects:
        obj = getattr(current_node.obj, obj_name)
        current_node.child = ObjectNode(obj, obj_name, parent=current_node)  # type: ignore
        current_node = current_node.child  # type: ignore

    leaf = current_node

    # We now try to get the "real" parent module, not the one the object was imported into.
    # This is important if we want to be able to retrieve the docstring of an attribute for example.
    # Once we find an object for which we could get the module, we stop trying to get the module.
    # Once we reach the node before the root, we apply the module if found, and break.
    real_module = None
    while current_node.parent is not None:
        if real_module is None:
            real_module = inspect.getmodule(current_node.obj)
        if inspect.ismodule(current_node.parent.obj):
            if real_module is not None and real_module is not current_node.parent.obj:
                current_node.parent = ObjectNode(real_module, real_module.__name__)
            break
        current_node = current_node.parent

    return leaf


class Loader:
    """
    This class contains the object documentation loading mechanisms.

    Any error that occurred during collection of the objects and their documentation is stored in the `errors` list.
    """

    def __init__(
        self,
        filters: Optional[List[str]] = None,
        docstring_style: str = "google",
        docstring_options: Optional[dict] = None,
        inherited_members: bool = False,
    ) -> None:
        """
        Initialization method.

        Arguments:
            filters: A list of regular expressions to fine-grain select members. It is applied recursively.
            docstring_style: The style to use when parsing docstrings.
            docstring_options: The options to pass to the docstrings parser.
            inherited_members: Whether to select inherited members for classes.
        """
        if not filters:
            filters = []

        self.filters = [(f, re.compile(f.lstrip("!"))) for f in filters]
        self.docstring_parser = PARSERS[docstring_style](**(docstring_options or {}))  # type: ignore
        self.errors: List[str] = []
        self.select_inherited_members = inherited_members

    def get_object_documentation(self, dotted_path: str, members: Optional[Union[Set[str], bool]] = None) -> Object:
        """
        Get the documentation for an object and its children.

        Arguments:
            dotted_path: The Python dotted path to the desired object.
            members: `True` to select members and filter them, `False` to select no members,
                or a list of names to explicitly select the members with these names.
                It is applied only on the root object.

        Return:
            The documented object.
        """
        if members is True:
            members = set()

        root_object: Object
        leaf = get_object_tree(dotted_path)

        if leaf.is_module():
            root_object = self.get_module_documentation(leaf, members)
        elif leaf.is_class():
            root_object = self.get_class_documentation(leaf, members)
        elif leaf.is_staticmethod():
            root_object = self.get_staticmethod_documentation(leaf)
        elif leaf.is_classmethod():
            root_object = self.get_classmethod_documentation(leaf)
        elif leaf.is_method():
            root_object = self.get_regular_method_documentation(leaf)
        elif leaf.is_function():
            root_object = self.get_function_documentation(leaf)
        elif leaf.is_property():
            root_object = self.get_property_documentation(leaf)
        else:
            root_object = self.get_attribute_documentation(leaf)

        root_object.parse_all_docstrings(self.docstring_parser)

        return root_object

    def get_module_documentation(self, node: ObjectNode, select_members=None) -> Module:
        """
        Get the documentation for a module and its children.

        Arguments:
            node: The node representing the module and its parents.
            select_members: Explicit members to select.

        Return:
            The documented module object.
        """
        module = node.obj
        path = node.dotted_path
        name = path.split(".")[-1]
        source: Optional[Source]

        try:
            source = Source(inspect.getsource(module), 1)
        except OSError as error:
            try:
                with Path(node.file_path).open() as fd:
                    code = fd.readlines()
                    if code:
                        source = Source(code, 1)
                    else:
                        source = None
            except OSError:
                self.errors.append(f"Couldn't read source for '{path}': {error}")
                source = None

        root_object = Module(
            name=name, path=path, file_path=node.file_path, docstring=inspect.getdoc(module), source=source
        )

        if select_members is False:
            return root_object

        # type_hints = get_type_hints(module)
        select_members = select_members or set()

        attributes_data = get_module_attributes(module)
        root_object.parse_docstring(self.docstring_parser, attributes=attributes_data)

        for member_name, member in inspect.getmembers(module):
            if self.select(member_name, select_members):  # type: ignore
                child_node = ObjectNode(member, member_name, parent=node)
                if child_node.is_class() and node.root.obj is inspect.getmodule(member):
                    root_object.add_child(self.get_class_documentation(child_node))
                elif child_node.is_function() and node.root.obj is inspect.getmodule(member):
                    root_object.add_child(self.get_function_documentation(child_node))
                elif member_name in attributes_data:
                    root_object.add_child(self.get_attribute_documentation(child_node, attributes_data[member_name]))

        try:
            package_path = module.__path__
        except AttributeError:
            pass
        else:
            for _, modname, _ in pkgutil.iter_modules(package_path):
                if self.select(modname, select_members):
                    leaf = get_object_tree(f"{path}.{modname}")
                    root_object.add_child(self.get_module_documentation(leaf))

        return root_object

    def get_class_documentation(self, node: ObjectNode, select_members=None) -> Class:
        """
        Get the documentation for a class and its children.

        Arguments:
            node: The node representing the class and its parents.
            select_members: Explicit members to select.

        Return:
            The documented class object.
        """
        class_ = node.obj
        docstring = inspect.cleandoc(class_.__doc__ or "")
        root_object = Class(name=node.name, path=node.dotted_path, file_path=node.file_path, docstring=docstring)

        # Even if we don't select members, we want to correctly parse the docstring
        attributes_data: Dict[str, Dict[str, Any]] = {}
        for cls in reversed(class_.__mro__[:-1]):
            merge(attributes_data, get_class_attributes(cls))
        context: Dict[str, Any] = {"attributes": attributes_data}
        if "__init__" in class_.__dict__:
            attributes_data.update(get_instance_attributes(class_.__init__))
            context["signature"] = inspect.signature(class_.__init__)
        root_object.parse_docstring(self.docstring_parser, attributes=attributes_data)

        if select_members is False:
            return root_object

        select_members = select_members or set()

        # Build the list of members
        members = {}
        inherited = set()
        direct_members = class_.__dict__
        all_members = dict(inspect.getmembers(class_))
        for member_name, member in all_members.items():
            if not (member is type or member is object) and self.select(member_name, select_members):
                if member_name not in direct_members:
                    if self.select_inherited_members:
                        members[member_name] = member
                        inherited.add(member_name)
                else:
                    members[member_name] = member

        # Iterate on the selected members
        child: Object
        for member_name, member in members.items():
            child_node = ObjectNode(member, member_name, parent=node)
            if child_node.is_class():
                child = self.get_class_documentation(child_node)
            elif child_node.is_classmethod():
                child = self.get_classmethod_documentation(child_node)
            elif child_node.is_staticmethod():
                child = self.get_staticmethod_documentation(child_node)
            elif child_node.is_method():
                child = self.get_regular_method_documentation(child_node)
            elif child_node.is_property():
                child = self.get_property_documentation(child_node)
            elif member_name in attributes_data:
                child = self.get_attribute_documentation(child_node, attributes_data[member_name])
            else:
                continue
            if member_name in inherited:
                child.properties.append("inherited")
            root_object.add_child(child)

        # First check if this is Pydantic compatible
        if "__fields__" in direct_members or (self.select_inherited_members and "__fields__" in all_members):
            root_object.properties = ["pydantic-model"]
            for field_name, model_field in all_members["__fields__"].items():
                if self.select(field_name, select_members) and (  # type: ignore
                    self.select_inherited_members
                    # When we don't select inherited members, one way to tell if a field was inherited
                    # is to check if it exists in parent classes __fields__ attributes.
                    # We don't check the current class, nor the top one (object), hence __mro__[1:-1]
                    or field_name not in chain(*(getattr(cls, "__fields__", {}).keys() for cls in class_.__mro__[1:-1]))
                ):
                    child_node = ObjectNode(obj=model_field, name=field_name, parent=node)
                    root_object.add_child(self.get_pydantic_field_documentation(child_node))

        # Check if this is a marshmallow class
        elif "_declared_fields" in direct_members or (
            self.select_inherited_members and "_declared_fields" in all_members
        ):
            root_object.properties = ["marshmallow-model"]
            for field_name, model_field in all_members["_declared_fields"].items():
                if self.select(field_name, select_members) and (  # type: ignore
                    self.select_inherited_members
                    # Same comment as for Pydantic models
                    or field_name
                    not in chain(*(getattr(cls, "_declared_fields", {}).keys() for cls in class_.__mro__[1:-1]))
                ):
                    child_node = ObjectNode(obj=model_field, name=field_name, parent=node)
                    root_object.add_child(self.get_marshmallow_field_documentation(child_node))

        # Handle dataclasses
        elif "__dataclass_fields__" in direct_members or (
            self.select_inherited_members and "__dataclass_fields__" in all_members
        ):
            root_object.properties = ["dataclass"]

            for field in all_members["__dataclass_fields__"].values():
                if self.select(field.name, select_members) and (  # type: ignore
                    self.select_inherited_members
                    # Same comment as for Pydantic models
                    or field.name
                    not in chain(*(getattr(cls, "__dataclass_fields__", {}).keys() for cls in class_.__mro__[1:-1]))
                ):
                    child_node = ObjectNode(obj=field.type, name=field.name, parent=node)
                    root_object.add_child(self.get_annotated_dataclass_field(child_node))

        return root_object

    def get_function_documentation(self, node: ObjectNode) -> Function:
        """
        Get the documentation for a function.

        Arguments:
            node: The node representing the function and its parents.

        Return:
            The documented function object.
        """
        function = node.obj
        path = node.dotted_path
        source: Optional[Source]
        signature: Optional[inspect.Signature]

        try:
            signature = inspect.signature(function)
        except TypeError as error:
            self.errors.append(f"Couldn't get signature for '{path}': {error}")
            signature = None

        try:
            source = Source(*inspect.getsourcelines(function))
        except OSError as error:
            self.errors.append(f"Couldn't read source for '{path}': {error}")
            source = None

        properties: List[str] = []
        if node.is_coroutine_function():
            properties.append("async")

        return Function(
            name=node.name,
            path=node.dotted_path,
            file_path=node.file_path,
            docstring=inspect.getdoc(function),
            signature=signature,
            source=source,
            properties=properties,
        )

    def get_property_documentation(self, node: ObjectNode) -> Attribute:
        """
        Get the documentation for an attribute.

        Arguments:
            node: The node representing the attribute and its parents.

        Return:
            The documented attribute object.
        """
        prop = node.obj
        path = node.dotted_path
        properties = ["property", "readonly" if prop.fset is None else "writable"]
        source: Optional[Source]

        try:
            signature = inspect.signature(prop.fget)
        except (TypeError, ValueError) as error:
            self.errors.append(f"Couldn't get signature for '{path}': {error}")
            attr_type = None
        else:
            attr_type = signature.return_annotation

        try:
            source = Source(*inspect.getsourcelines(prop.fget))
        except (OSError, TypeError) as error:
            self.errors.append(f"Couldn't get source for '{path}': {error}")
            source = None

        return Attribute(
            name=node.name,
            path=path,
            file_path=node.file_path,
            docstring=inspect.getdoc(prop.fget),
            attr_type=attr_type,
            properties=properties,
            source=source,
        )

    @staticmethod
    def get_pydantic_field_documentation(node: ObjectNode) -> Attribute:
        """
        Get the documentation for a Pydantic Field.

        Arguments:
            node: The node representing the Field and its parents.

        Return:
            The documented attribute object.
        """
        prop = node.obj
        path = node.dotted_path
        properties = ["pydantic-field"]
        if prop.required:
            properties.append("required")

        return Attribute(
            name=node.name,
            path=path,
            file_path=node.file_path,
            docstring=prop.field_info.description,
            attr_type=prop.type_,
            properties=properties,
        )

    @staticmethod
    def get_marshmallow_field_documentation(node: ObjectNode) -> Attribute:
        """
        Get the documentation for a Marshmallow Field.

        Arguments:
            node: The node representing the Field and its parents.

        Return:
            The documented attribute object.
        """
        prop = node.obj
        path = node.dotted_path
        properties = ["marshmallow-field"]
        if prop.required:
            properties.append("required")

        return Attribute(
            name=node.name,
            path=path,
            file_path=node.file_path,
            docstring=prop.metadata.get("description"),
            attr_type=type(prop),
            properties=properties,
        )

    @staticmethod
    def get_annotated_dataclass_field(node: ObjectNode, attribute_data: Optional[dict] = None) -> Attribute:
        """
        Get the documentation for an dataclass annotation.

        Arguments:
            node: The node representing the annotation and its parents.
            attribute_data: Docstring and annotation for this attribute.

        Return:
            The documented attribute object.
        """
        if attribute_data is None:
            if node.parent_is_class():
                attribute_data = get_class_attributes(node.parent.obj).get(node.name, {})  # type: ignore
            else:
                attribute_data = get_module_attributes(node.root.obj).get(node.name, {})

        return Attribute(
            name=node.name,
            path=node.dotted_path,
            file_path=node.file_path,
            docstring=attribute_data["docstring"],
            attr_type=attribute_data["annotation"],
            properties=["dataclass-field"],
        )

    def get_classmethod_documentation(self, node: ObjectNode) -> Method:
        """
        Get the documentation for a class-method.

        Arguments:
            node: The node representing the class-method and its parents.

        Return:
            The documented method object.
        """
        return self.get_method_documentation(node, ["classmethod"])

    def get_staticmethod_documentation(self, node: ObjectNode) -> Method:
        """
        Get the documentation for a static-method.

        Arguments:
            node: The node representing the static-method and its parents.

        Return:
            The documented method object.
        """
        return self.get_method_documentation(node, ["staticmethod"])

    def get_regular_method_documentation(self, node: ObjectNode) -> Method:
        """
        Get the documentation for a regular method (not class- nor static-method).

        We do extra processing in this method to discard docstrings of `__init__` methods
        that were inherited from parent classes.

        Arguments:
            node: The node representing the method and its parents.

        Return:
            The documented method object.
        """
        method = self.get_method_documentation(node)
        if node.parent:
            class_ = node.parent.obj
            if RE_SPECIAL.match(node.name):
                docstring = method.docstring
                parent_classes = class_.__mro__[1:]
                for parent_class in parent_classes:
                    try:
                        parent_method = getattr(parent_class, node.name)
                    except AttributeError:
                        continue
                    else:
                        if docstring == inspect.getdoc(parent_method):
                            method.docstring = ""
                        break
        return method

    def get_method_documentation(self, node: ObjectNode, properties: Optional[List[str]] = None) -> Method:
        """
        Get the documentation for a method.

        Arguments:
            node: The node representing the method and its parents.
            properties: A list of properties to apply to the method.

        Return:
            The documented method object.
        """
        method = node.obj
        path = node.dotted_path
        source: Optional[Source]

        try:
            source = Source(*inspect.getsourcelines(method))
        except OSError as error:
            self.errors.append(f"Couldn't read source for '{path}': {error}")
            source = None
        except TypeError:
            source = None

        if node.is_coroutine_function():
            if properties is None:
                properties = ["async"]
            else:
                properties.append("async")

        return Method(
            name=node.name,
            path=path,
            file_path=node.file_path,
            docstring=inspect.getdoc(method),
            signature=inspect.signature(method),
            properties=properties or [],
            source=source,
        )

    @staticmethod
    def get_attribute_documentation(node: ObjectNode, attribute_data: Optional[dict] = None) -> Attribute:
        """
        Get the documentation for an attribute.

        Arguments:
            node: The node representing the method and its parents.
            attribute_data: Docstring and annotation for this attribute.

        Returns:
            The documented attribute object.
        """
        if attribute_data is None:
            if node.parent_is_class():
                attribute_data = get_class_attributes(node.parent.obj).get(node.name, {})  # type: ignore
            else:
                attribute_data = get_module_attributes(node.root.obj).get(node.name, {})
        return Attribute(
            name=node.name,
            path=node.dotted_path,
            file_path=node.file_path,
            docstring=attribute_data.get("docstring", ""),
            attr_type=attribute_data.get("annotation", None),
        )

    def select(self, name: str, names: Set[str]) -> bool:
        """
        Tells whether we should select an object or not, given its name.

        If the set of names is not empty, we check against it, otherwise we check against filters.

        Arguments:
            name: The name of the object to select or not.
            names: An explicit list of names to select.

        Returns:
            Yes or no.
        """
        if names:
            return name in names
        return not self.filter_name_out(name)

    @lru_cache(maxsize=None)
    def filter_name_out(self, name: str) -> bool:
        """
        Filter a name based on the loader's filters.

        Arguments:
            name: The name to filter.

        Returns:
            True if the name was filtered out, False otherwise.
        """
        if not self.filters:
            return False
        keep = True
        for fltr, regex in self.filters:
            is_matching = bool(regex.search(name))
            if is_matching:
                if str(fltr).startswith("!"):
                    is_matching = not is_matching
                keep = is_matching
        return not keep
