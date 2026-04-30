"""
CoT Math Norm Refiner — Rule-based LaTeX / formula normalizer
=============================================================
A **zero-LLM** post-processing operator that standardises mathematical
notation inside CoT text.  Designed as a lightweight stage that can be
appended to Method A or Method D outputs (which do not produce uniformly
standard LaTeX the way Method C does).

Scope
-----
Operates only inside math regions (inline ``$…$``, display ``$$…$$``,
``\\[…\\]``, ``\\(…\\)`` delimiters).  Plain prose is never modified.

Normalisation rules applied
----------------------------
1.  **\\frac fixup**  – ``\\frac ab`` / ``\\frac{a}b`` → ``\\frac{a}{b}``
2.  **\\sqrt fixup**  – ``\\sqrt x`` / ``\\sqrt2`` → ``\\sqrt{x}``
3.  **Alias collapse**
    * ``\\tfrac``, ``\\dfrac``  → ``\\frac``
    * ``\\neq`` → ``\\ne``
    * ``\\leq`` → ``\\le``,  ``\\geq`` → ``\\ge``
    * ``\\left(`` / ``\\right)`` etc. → ``(`` / ``)``   *(optional, off by default)*
4.  **Matrix environment** – ``\\begin{array}{…}…\\end{array}`` →
    ``\\begin{pmatrix}…\\end{pmatrix}``  (and ``bmatrix`` → ``pmatrix``)
5.  **Operator spacing**  – removes ``\\!`` (negative thin space) inside math
6.  **Redundant ``\\text`` wrappers** – ``\\text{foo}`` → ``foo`` inside math
7.  **Degree symbol** – ``^{\\circ}`` / ``^\\circ`` → ``°``  *(optional)*
8.  **Inline fraction shorthand** – bare ``a/b`` (integers only) →
    ``\\frac{a}{b}``  *(optional, off by default to avoid false positives)*
9.  **Chemical formula pass-through** – patterns like ``H_2O`` / ``CO_2``
    are left unchanged (rule-based chem normalisation is out of scope).

All rules are configurable via constructor flags so the caller can
enable/disable individual transformations.

Usage
-----
::

    from dataflow.operators.reasoning import CoTMathNormRefiner
    op = CoTMathNormRefiner()
    output_cols = op.run(storage, input_key="cot_cleaned", output_key="cot_normed")
"""

import re
from typing import Optional

import pandas as pd

from dataflow import get_logger
from dataflow.core import OperatorABC
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# Matches inline math:  $...$  (non-greedy, no nested $)
_RE_INLINE = re.compile(r"\$(?!\$)(.*?)\$", re.DOTALL)
# Matches display math: $$...$$ or \[...\]
_RE_DISPLAY_DOLLAR = re.compile(r"\$\$(.*?)\$\$", re.DOTALL)
_RE_DISPLAY_BRACKET = re.compile(r"\\\[(.*?)\\\]", re.DOTALL)
# Matches \(...\)
_RE_PAREN_MATH = re.compile(r"\\\((.*?)\\\)", re.DOTALL)


def _apply_to_math_regions(text: str, fn) -> str:
    """
    Apply *fn(math_content) → new_math_content* to every math region in *text*,
    preserving the surrounding delimiters.  Processes display math first (to
    avoid mis-identifying ``$$`` as two consecutive ``$``).
    """
    # Display $$...$$
    def _repl_display_dollar(m):
        return "$$" + fn(m.group(1)) + "$$"
    text = _RE_DISPLAY_DOLLAR.sub(_repl_display_dollar, text)

    # Display \[...\]
    def _repl_display_bracket(m):
        return "\\[" + fn(m.group(1)) + "\\]"
    text = _RE_DISPLAY_BRACKET.sub(_repl_display_bracket, text)

    # \(...\)
    def _repl_paren(m):
        return "\\(" + fn(m.group(1)) + "\\)"
    text = _RE_PAREN_MATH.sub(_repl_paren, text)

    # Inline $...$
    def _repl_inline(m):
        return "$" + fn(m.group(1)) + "$"
    text = _RE_INLINE.sub(_repl_inline, text)

    return text


# ---------------------------------------------------------------------------
# Individual normalisation functions (operate on raw math content, no $)
# ---------------------------------------------------------------------------

def _fix_frac(s: str) -> str:
    r"""
    ``\frac ab`` / ``\frac{a}b`` / ``\frac a{b}`` → ``\frac{a}{b}``

    Only fixes cases where at least one argument is un-braced and is a
    single non-whitespace, non-backslash, non-brace character.
    """
    # Character class for a single "simple" token: not {, }, whitespace, or backslash
    # We use [^ {}\\t\\n\\r\\f\\v\\\\] which in raw string is [^ {}\t\n\r\f\v\\]
    _SC = r"[^ {}\t\n\r\f\v\\]"

    # \frac<ws?><sc><sc>  (neither braced, optional whitespace after \frac)
    s = re.sub(
        r"\\frac\s*(" + _SC + r")\s*(" + _SC + r")",
        lambda m: rf"\frac{{{m.group(1)}}}{{{m.group(2)}}}",
        s,
    )
    # \frac{...}<ws?><sc>  (second arg un-braced)
    s = re.sub(
        r"\\frac(\{[^}]*\})\s*(" + _SC + r")",
        lambda m: rf"\frac{m.group(1)}{{{m.group(2)}}}",
        s,
    )
    # \frac<ws?><sc>{...}  (first arg un-braced)
    s = re.sub(
        r"\\frac\s*(" + _SC + r")(\{[^}]*\})",
        lambda m: rf"\frac{{{m.group(1)}}}{m.group(2)}",
        s,
    )
    return s


def _fix_sqrt(s: str) -> str:
    r"""``\sqrt x`` / ``\sqrt2`` → ``\sqrt{x}``  (single word-char argument)."""
    return re.sub(r"\\sqrt\s*(?!\{)(\w)", r"\\sqrt{\1}", s)


def _fix_aliases(s: str) -> str:
    """Collapse common LaTeX alias variants to canonical forms."""
    s = s.replace("\\tfrac", "\\frac").replace("\\dfrac", "\\frac")
    s = s.replace("\\neq", "\\ne")
    s = s.replace("\\leq", "\\le")
    s = s.replace("\\geq", "\\ge")
    return s


def _fix_matrix(s: str) -> str:
    r"""``\begin{array}{...}`` → ``\begin{pmatrix}``."""
    s = re.sub(r"\\begin\{array\}\{[^}]*\}", r"\\begin{pmatrix}", s)
    s = re.sub(r"\\end\{array\}", r"\\end{pmatrix}", s)
    s = s.replace("\\begin{bmatrix}", "\\begin{pmatrix}")
    s = s.replace("\\end{bmatrix}", "\\end{pmatrix}")
    return s


def _fix_spacing(s: str) -> str:
    r"""Remove ``\!`` (negative thin space) that adds no semantic value."""
    return s.replace("\\!", "")


def _fix_text_wrapper(s: str) -> str:
    r"""``\text{foo}`` → ``foo`` inside math (removes wrapper, keeps content)."""
    return re.sub(r"\\text\{([^}]*)\}", r"\1", s)


def _fix_degree(s: str) -> str:
    r"""``^{\circ}`` / ``^\circ`` → ``°``."""
    s = re.sub(r"\^\{\\circ\}", "°", s)
    s = re.sub(r"\^\\circ\b", "°", s)
    return s


def _fix_remove_lr(s: str) -> str:
    r"""Remove ``\left`` / ``\right`` size hints (optional)."""
    s = re.sub(r"\\left\s*", "", s)
    s = re.sub(r"\\right\s*", "", s)
    return s


def _fix_integer_fraction(s: str) -> str:
    r"""``3/4`` (integers only) → ``\frac{3}{4}`` (optional, off by default)."""
    return re.sub(
        r"(?<![\\{/\w])(-?\d+)\s*/\s*(-?\d+)(?![/\w}])",
        lambda m: rf"\frac{{{m.group(1)}}}{{{m.group(2)}}}",
        s,
    )


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

@OPERATOR_REGISTRY.register()
class CoTMathNormRefiner(OperatorABC):
    """
    Rule-based LaTeX / math formula normaliser for CoT text.

    Applies a configurable set of regex-based transformations **only inside
    math regions** (``$…$``, ``$$…$$``, ``\\[…\\]``, ``\\(…\\)``).
    Prose text is never touched.  No LLM calls are made.

    Parameters
    ----------
    fix_frac : bool
        Fix un-braced ``\\frac`` arguments.  Default: True.
    fix_sqrt : bool
        Fix un-braced ``\\sqrt`` arguments.  Default: True.
    fix_aliases : bool
        Collapse ``\\tfrac``/``\\dfrac`` → ``\\frac``, ``\\neq`` → ``\\ne``,
        ``\\leq`` → ``\\le``, ``\\geq`` → ``\\ge``.  Default: True.
    fix_matrix : bool
        Rewrite ``array`` / ``bmatrix`` environments to ``pmatrix``.
        Default: True.
    fix_spacing : bool
        Remove ``\\!`` negative thin spaces.  Default: True.
    fix_text_wrapper : bool
        Strip ``\\text{…}`` wrappers inside math.  Default: True.
    fix_degree : bool
        Replace ``^{\\circ}`` with ``°``.  Default: False.
    fix_remove_lr : bool
        Remove ``\\left`` / ``\\right`` size hints.  Default: False.
    fix_integer_fraction : bool
        Convert bare ``a/b`` (integers) to ``\\frac{a}{b}``.  Default: False.
    """

    def __init__(
        self,
        fix_frac: bool = True,
        fix_sqrt: bool = True,
        fix_aliases: bool = True,
        fix_matrix: bool = True,
        fix_spacing: bool = True,
        fix_text_wrapper: bool = True,
        fix_degree: bool = False,
        fix_remove_lr: bool = False,
        fix_integer_fraction: bool = False,
    ):
        self.logger = get_logger()
        self.fix_frac = fix_frac
        self.fix_sqrt = fix_sqrt
        self.fix_aliases = fix_aliases
        self.fix_matrix = fix_matrix
        self.fix_spacing = fix_spacing
        self.fix_text_wrapper = fix_text_wrapper
        self.fix_degree = fix_degree
        self.fix_remove_lr = fix_remove_lr
        self.fix_integer_fraction = fix_integer_fraction

    # ── description ──────────────────────────────────────────────────────

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "规则型 LaTeX / 数学公式标准化算子（无 LLM 调用）。\\n"
                "仅对 CoT 文本中的数学区域（$…$、$$…$$、\\[…\\]、\\(…\\)）"
                "做正则替换，不修改普通文本。\\n"
                "覆盖规则：\\\\frac / \\\\sqrt 参数补全大括号、别名折叠"
                "（tfrac/dfrac→frac、\\\\neq→\\\\ne 等）、矩阵环境统一为 pmatrix、"
                "删除 \\\\! 间距、\\\\text\\{\\} wrapper 展开，以及可选的"
                "度数符号替换和 \\\\left/\\\\right 删除。\\n"
                "适合作为 Method A / Method D 后的轻量后处理步骤。"
            )
        else:
            return (
                "Rule-based LaTeX / math formula normaliser (zero LLM calls).\\n"
                "Applies regex transformations only inside math regions "
                "($…$, $$…$$, \\[…\\], \\(…\\)); prose is never modified.\\n"
                "Rules: fix un-braced \\\\frac / \\\\sqrt args, collapse aliases "
                "(tfrac/dfrac→frac, \\\\neq→\\\\ne, etc.), unify matrix envs to "
                "pmatrix, strip \\\\! spacing, unwrap \\\\text{}, and optional "
                "degree-symbol replacement and \\\\left/\\\\right removal.\\n"
                "Intended as a lightweight post-processing step after Method A / D."
            )

    # ── validation ───────────────────────────────────────────────────────

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        missing = [c for c in [self.input_key] if c not in dataframe.columns]
        if missing:
            raise ValueError(
                f"[{self.__class__.__name__}] Missing required column(s): {missing}"
            )

    # ── core processing ──────────────────────────────────────────────────

    def _build_transform(self):
        """Return a function that applies all enabled rules to math content."""
        rules = []
        if self.fix_aliases:
            rules.append(_fix_aliases)
        if self.fix_frac:
            rules.append(_fix_frac)
        if self.fix_sqrt:
            rules.append(_fix_sqrt)
        if self.fix_matrix:
            rules.append(_fix_matrix)
        if self.fix_spacing:
            rules.append(_fix_spacing)
        if self.fix_text_wrapper:
            rules.append(_fix_text_wrapper)
        if self.fix_degree:
            rules.append(_fix_degree)
        if self.fix_remove_lr:
            rules.append(_fix_remove_lr)
        if self.fix_integer_fraction:
            rules.append(_fix_integer_fraction)

        def _transform(math_content: str) -> str:
            for rule in rules:
                math_content = rule(math_content)
            return math_content

        return _transform

    def _normalise_text(self, text: str, transform) -> str:
        """Apply *transform* to all math regions in *text*."""
        return _apply_to_math_regions(text, transform)

    # ── run ──────────────────────────────────────────────────────────────

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "cot_cleaned",
        output_key: str = "cot_normed",
    ) -> list[str]:
        """
        Normalise LaTeX notation in CoT column.

        Parameters
        ----------
        storage : DataFlowStorage
        input_key : str
            Column containing CoT text (plain or ``<think>``-wrapped).
            Default: ``"cot_cleaned"``.
        output_key : str
            Column to write normalised CoT into.  Default: ``"cot_normed"``.

        Returns
        -------
        list[str]
            Output column names: ``[output_key]``.
        """
        self.input_key = input_key
        dataframe = storage.read("dataframe")
        self._validate_dataframe(dataframe)

        transform = self._build_transform()

        normed: list[str] = []
        changed = 0
        for _, row in dataframe.iterrows():
            raw = str(row[input_key]) if pd.notna(row[input_key]) else ""
            result = self._normalise_text(raw, transform)
            normed.append(result)
            if result != raw:
                changed += 1

        dataframe[output_key] = normed
        output_file = storage.write(dataframe)

        self.logger.info(
            f"[{self.__class__.__name__}] Normalised {changed}/{len(dataframe)} rows. "
            f"Saved to {output_file}"
        )
        return [output_key]
