import re

import penman

from scheme import amrutil
from ucca import layer0, layer1, convert, textutil


COMMENT_PREFIX = "#"
ID_PATTERN = "#\s*::id\s+(\S+)"
TOK_PATTERN = "#\s*::(?:tok|snt)\s+(.*)"
DEP_PREFIX = ":"
TOP_DEP = ":top"
DEP_REPLACEMENT = {amrutil.INSTANCE_OF: "instance"}
ALIGNMENT_PREFIX = "e."
ALIGNMENT_SEP = ","
LAYERS = {"wiki": ("wiki",),
          "numbers": ()}


class AmrConverter(convert.FormatConverter):
    def __init__(self):
        self.passage_id = self.amr_id = self.lines = self.tokens = self.nodes = self.return_amr = \
            self.remove_cycles = self.layers = None

    def from_format(self, lines, passage_id, return_amr=False, remove_cycles=True, **kwargs):
        self.passage_id = passage_id
        self.return_amr = return_amr
        self.remove_cycles = remove_cycles
        self.layers = [l for l in LAYERS if kwargs.get(l)]
        self.lines = []
        self.amr_id = self.tokens = None
        for line in lines:
            line = line.lstrip()
            if line:
                if line[0] != COMMENT_PREFIX:
                    self.lines.append(line)
                    continue
                m = re.match(ID_PATTERN, line)
                if m:
                    self.amr_id = m.group(1)
                else:
                    m = re.match(TOK_PATTERN, line)
                    if m:
                        self.tokens = [t.strip("@") for t in re.sub("\\\\|(?<=<)[^<>]+(?=>)", "", m.group(1)).split()]
            if self.lines:
                yield self._build_passage()
        if self.lines:
            yield self._build_passage()

    def _build_passage(self):
        # amr = penman.decode(re.sub("~e\.[\d,]+", "", " ".join(self.lines)))
        amr = amrutil.parse(" ".join(self.lines), tokens=self.tokens)
        passage = next(convert.from_text(self.tokens, self.amr_id or self.passage_id, tokenized=True))
        self.lines = []
        self.amr_id = self.tokens = None
        textutil.annotate(passage)
        l1 = passage.layer(layer1.LAYER_ID)
        self._build_layer1(amr, l1)
        self._build_layer0(self.align_nodes(amr), l1, passage.layer(layer0.LAYER_ID))
        self._update_implicit(l1)
        self._update_labels(l1)
        # return (passage, penman.encode(amr), self.amr_id) if self.return_amr else passage
        return (passage, amr(alignments=False), self.amr_id) if self.return_amr else passage

    def _build_layer1(self, amr, l1):
        def _reachable(x, y):  # is there a path from x to y? used to detect cycles
            q = [x]
            v = set()
            while q:
                x = q.pop(0)
                if x in v:
                    continue
                v.add(x)
                if x == y:
                    return True
                q += [d for _, _, d in amr.triples(head=x)]
            return False

        top = amr.triples(rel=TOP_DEP)  # start breadth-first search from :top relation
        assert len(top) == 1, "There must be exactly one %s edge, but %d are found" % (TOP_DEP, len(top))
        _, _, root = top[0]  # init with child of TOP
        pending = amr.triples(head=root)
        self.nodes = {}  # map triples to UCCA nodes: dep gets a new node each time unless it's a variable
        l1.top_node.attrib[amrutil.LABEL_ATTRIB] = amrutil.VARIABLE_LABEL  # the root is always a variable
        variables = {root: l1.top_node}  # map AMR variables to UCCA nodes
        visited = set()  # to avoid cycles
        while pending:  # breadth-first search creating layer 1 nodes
            triple = pending.pop(0)
            if triple in visited:
                continue
            visited.add(triple)
            head, rel, dep = triple
            rel = rel.lstrip(DEP_PREFIX)
            for layer, rels in LAYERS.items():
                if layer not in self.layers and rel in rels:
                    continue
            parent = variables.get(head)
            assert parent is not None, "Outgoing edge from a non-variable: " + str(triple)
            node = variables.get(dep)
            if node is None:  # first occurrence of dep, or dep is not a variable
                pending += amr.triples(head=dep)  # to continue breadth-first search
                node = l1.add_fnode(parent, rel)
                if isinstance(dep, amrutil.amr_lib.Var):
                    variables[dep] = node
                    label = amrutil.VARIABLE_LABEL
                else:  # save concept name / constant value in node attributes
                    label = repr(dep)
                node.attrib[amrutil.LABEL_ATTRIB] = label
            elif not self.remove_cycles or not _reachable(dep, head):  # reentrancy; do not add if results in a cycle
                l1.add_remote(parent, rel, node)
            self.nodes[triple] = node

    @staticmethod
    def _build_layer0(preterminals, l1, l0):  # add edges to terminals according to alignments
        for i, parents in preterminals.items():
            terminal = l0.all[i]
            if layer0.is_punct(terminal):
                tag = layer1.EdgeTags.Punctuation
                terminal = l1.add_punct(parents[0], terminal)
                terminal.attrib[amrutil.LABEL_ATTRIB] = layer1.NodeTags.Punctuation
            else:
                tag = layer1.EdgeTags.Terminal
                parents[0].add(tag, terminal)
            for parent in parents[1:]:  # add as remote terminal child to all parents but the first
                if parent not in terminal.parents:  # avoid multiple identical edges (e.g. :polarity~e.68 -~e.68)
                    l1.add_remote(parent, tag, terminal)

    def align_nodes(self, amr):
        preterminals = {}
        alignments = (amr.alignments(), amr.role_alignments())
        tokens = amr.tokens()
        for triple, node in self.nodes.items():
            indices = []
            for alignment in alignments:
                align = alignment.get(triple)
                if align is not None:
                    indices += list(map(int, align.lstrip(ALIGNMENT_PREFIX).split(ALIGNMENT_SEP)))  # split numeric
            # correct missing alignment by expanding to neighboring terminals contained in label
            label = str(triple[2])
            if indices:
                for start, offset in ((0, -1), (-1, 1)):
                    i = indices[start] + offset
                    while 0 <= i < len(tokens) and tokens[i] in label:
                        indices.append(i)
                        i += offset
            # also expand to any contained token if it is not too short and it occurs only once
            for i, token in enumerate(tokens):
                if i not in indices and len(token) > 2 and \
                                token in self.strip(label).strip('"') and tokens.count(token) == 1:
                    indices.append(i)
            for i in indices:
                preterminals.setdefault(i, []).append(node)
        return preterminals

    @staticmethod
    def _update_implicit(l1):
        # set implicit attribute for nodes with no terminal descendants
        pending = [n for n in l1.all if not n.children]
        while pending:
            node = pending.pop(0)
            if node in l1.heads:
                pass
            elif any(n in pending for n in node.children):
                pending.append(node)
            elif all(n.attrib.get("implicit") for n in node.children):
                node.attrib["implicit"] = True
                pending += node.parents

    def _update_labels(self, l1):
        for node in l1.all:
            label = amrutil.resolve_label(node, reverse=True)
            if "numbers" not in self.layers and label and label.startswith("Num("):
                label = re.sub("\([^<]+<", "(<", label)
                label = re.sub(">[^>]+\)", ">)", label)
                label = re.sub("\([\d.,]+\)", "(1)", label)
            node.attrib[amrutil.LABEL_ATTRIB] = label

    def to_format(self, passage, **kwargs):
        del kwargs
        textutil.annotate(passage)
        return penman.encode(penman.Graph(list(self._to_triples(passage)))),

    @staticmethod
    def _to_triples(passage):
        class _IdGenerator:
            def __init__(self):
                self._id = 0

            def __call__(self, label):
                if label == amrutil.VARIABLE_LABEL:
                    self._id += 1
                    return label + str(self._id)
                return label

        def _node_label(node):
            return AmrConverter.strip(labels.setdefault(node.ID, id_gen(amrutil.resolve_label(node))))

        id_gen = _IdGenerator()
        pending = list(passage.layer(layer1.LAYER_ID).top_node)
        visited = set()  # to avoid cycles
        labels = {}
        while pending:
            edge = pending.pop(0)
            if edge not in visited and edge.tag not in amrutil.TERMINAL_TAGS:  # skip cycles and terminals
                visited.add(edge)
                pending += edge.child
                tag = DEP_REPLACEMENT.get(edge.tag, edge.tag)
                yield _node_label(edge.parent), tag, _node_label(edge.child)

    @staticmethod
    def strip(label):
        return re.sub("\w+\((.*)\)", "\\1", label)


def from_amr(lines, passage_id=None, return_amr=False, *args, **kwargs):
    """Converts from parsed text in AMR PENMAN format to a Passage object.

    :param lines: iterable of lines in AMR PENMAN format, describing a single passage.
    :param passage_id: ID to set for passage, overriding the ID from the file
    :param return_amr: return triple of (UCCA passage, AMR string, AMR ID)

    :return generator of Passage objects
    """
    del args, kwargs
    return AmrConverter().from_format(lines, passage_id, return_amr)


def to_amr(passage, *args, **kwargs):
    """ Convert from a Passage object to a string in AMR PENMAN format (export)

    :param passage: the Passage object to convert

    :return list of lines representing an AMR in PENMAN format, constructed from the passage
    """
    del args, kwargs
    return AmrConverter().to_format(passage)


CONVERTERS = dict(convert.CONVERTERS)
CONVERTERS["amr"] = (from_amr, to_amr)