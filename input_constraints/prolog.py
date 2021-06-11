import logging
import re
import sys
import tempfile
import time
from typing import List, Dict, Tuple, Type, Callable, Union, Optional, Set

import pyswip
import z3
from fuzzingbook.GrammarCoverageFuzzer import GrammarCoverageFuzzer
from fuzzingbook.Grammars import is_nonterminal
from fuzzingbook.Parser import non_canonical, canonical
from grammar_graph.gg import GrammarGraph, NonterminalNode
from orderedset import OrderedSet
from pyswip import Prolog, registerForeign

import input_constraints.isla as isla
import input_constraints.prolog_shortcuts as psc
import input_constraints.prolog_structs as pl
from input_constraints.helpers import visit_z3_expr, is_canonical_grammar, is_z3_var, pyswip_output_to_str, \
    pyswip_output_to_python, python_to_prolog_tree, python_list_to_prolog_list, var_to_pl_nsym
from input_constraints.type_defs import CanonicalGrammar, Grammar

# A TranslationResult for a constraint is a list of Prolog rules together with a list of foreign foreign predicates,
# each consisting of the Python function for the predicate, the predicate name, and its arity.
ForeignFunctionSpec = Tuple[Callable, str, int]
TranslationResult = Tuple[List[pl.Rule], List[ForeignFunctionSpec]]


class Translator:
    # TODO: Make configurable
    FUZZING_DEPTH_ATOMIC_STRING_NONTERMINALS = 100

    def __init__(self,
                 grammar: Union[Grammar, CanonicalGrammar],
                 formula: isla.Formula,
                 numeric_nonterminals: Optional[Dict[str, Tuple[int, int]]] = None,
                 atomic_string_nonterminals: Optional[Dict[str, int]] = None
                 ):
        if is_canonical_grammar(grammar):
            self.grammar = grammar
        else:
            self.grammar = canonical(grammar)

        self.formula = formula

        self.used_variables: OrderedSet[isla.Variable] = isla.VariablesCollector(formula).collect()
        self.isla_to_prolog_var_map: Dict[isla.Variable, pl.Variable] = \
            {iv: self.to_prolog_var(iv) for iv in self.used_variables}
        self.isla_var_name_to_prolog_var_map: Dict[isla.Variable, pl.Variable] = \
            {iv.name: pv for iv, pv in self.isla_to_prolog_var_map.items()}
        self.predicate_map: Dict[str, str] = self.compute_predicate_names_for_nonterminals()

        self.numeric_nonterminals: Dict[str, Tuple[int, int]] = numeric_nonterminals \
            if numeric_nonterminals is not None \
            else self.compute_numeric_nonterminals()
        self.atomic_string_nonterminals: Dict[str, int] = atomic_string_nonterminals \
            if atomic_string_nonterminals is not None \
            else self.compute_atomic_string_nonterminals()

        self.logger = logging.getLogger(type(self).__name__)

    def translate(self) -> Prolog:
        def numeric_nonterminal(atom: pyswip.easy.Atom) -> bool:
            return f"<{atom.value}>" in self.numeric_nonterminals

        def atomic_string_nonterminal(atom: pyswip.easy.Atom) -> bool:
            return f"<{atom.value}>" in self.atomic_string_nonterminals

        fuzz_results: Dict[str, List[str]] = {}
        fuzzers: Dict[str, GrammarCoverageFuzzer] = {}

        def fuzz(atom: pyswip.easy.Atom, idx: int, result: pyswip.easy.Variable) -> bool:
            nonterminal = f"<{atom.value}>"

            grammar = GrammarGraph.from_grammar(non_canonical(self.grammar)).subgraph(nonterminal).to_grammar()
            fuzzer = fuzzers.setdefault(nonterminal, GrammarCoverageFuzzer(grammar))
            fuzz_results.setdefault(nonterminal, [])

            while len(fuzz_results[nonterminal]) <= idx:
                fuzz_results[nonterminal].append(fuzzer.fuzz())

            result.value = fuzz_results[nonterminal][idx]
            return True

        prolog = Prolog()

        try:
            import importlib.resources as pkg_resources
        except ImportError:
            # Try backported to PY<37 `importlib_resources`.
            import importlib_resources as pkg_resources

        preamble = pkg_resources.read_text(__package__, 'prolog_defs.pl')

        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(preamble.encode())
            prolog.consult(tmp.name)

        next(prolog.query("use_module(library(clpfd))"))

        for pred in ["atomic_nonterminal/1", "atomic_string_nonterminal/1", "fuzz/3"]:
            next(prolog.query(f"abolish({pred})"))

        registerForeign(numeric_nonterminal, name="numeric_nonterminal", arity=1)
        registerForeign(atomic_string_nonterminal, name="atomic_string_nonterminal", arity=1)
        registerForeign(fuzz, name="fuzz", arity=3)

        pl_grammar = self.translate_grammar()
        for rule in pl_grammar:
            prolog.assertz(str(rule))

        try:
            rules, foreign_functions = self.translate_constraint(self.formula)
            for rule in rules:
                prolog.assertz(str(rule))
            for func, name, arity in foreign_functions:
                registerForeign(func, name=name, arity=arity)
        except NotImplementedError as e:
            # TODO: Remove. Only for testing during ongoing development
            self.logger.warning(f"Translation method not implemented: {str(e)}")

        return prolog

    def translate_constraint(self, formula: isla.Formula, counter: int = 0) -> TranslationResult:
        translation_methods: Dict[Type[isla.Formula], Type[Callable[[isla.Formula, int], TranslationResult]]] = {
            isla.SMTFormula: self.translate_smt_formula,
            isla.PredicateFormula: self.translate_predicate_formula,
            isla.DisjunctiveFormula: self.translate_propositional_combinator,
            isla.ConjunctiveFormula: self.translate_propositional_combinator,
            isla.ForallFormula: self.translate_quantified_formula,
            isla.ExistsFormula: self.translate_quantified_formula,
        }

        if type(formula) not in translation_methods:
            raise NotImplementedError(f"Translation for '{type(formula).__name__}' not implemented.")

        return translation_methods[type(formula)](formula, counter)

    def translate_quantified_formula(self, formula: isla.QuantifiedFormula, counter: int) -> TranslationResult:
        head, isla_to_pl_vars_mapping, free_pl_vars, result_var, all_pl_vars = self.create_head(formula, counter)
        goals: List[pl.Goal] = []

        in_var_path_var = self.fresh_variable("InVarPath", all_pl_vars)
        in_var_tree_var = self.fresh_variable("InVarTree", all_pl_vars)
        qfd_var_paths = self.fresh_variable("QfdVarPaths", all_pl_vars)

        goals.append(psc.unify(psc.pair(in_var_path_var, in_var_tree_var),
                               isla_to_pl_vars_mapping[formula.in_variable]))

        rel_paths = None
        if formula.bind_expression is not None:
            prefix_tree, rel_paths = formula.bind_expression.to_tree_prefix(formula.bound_variable.n_type,
                                                                            non_canonical(self.grammar))
            pl_tree = python_to_prolog_tree(prefix_tree)
            goals.append(psc.pred("find_subtrees", in_var_tree_var, pl_tree, qfd_var_paths))
        else:
            goals.append(psc.pred("find_subtrees",
                                  in_var_tree_var,
                                  psc.list_term(var_to_pl_nsym(formula.bound_variable), psc.anon_var()),
                                  qfd_var_paths))

        qfd_var_rel_path_var = self.fresh_variable("QfdVarRelPath", all_pl_vars)
        qfd_var_path_var = self.fresh_variable("QfdVarPath", all_pl_vars)
        out_var = self.fresh_variable("Out", all_pl_vars)
        sub_results = self.fresh_variable("SubResults", all_pl_vars)

        lambda_goals: List[pl.Goal] = []

        isla_to_pl_vars_mapping.update(
            {v: self.fresh_variable(v.name, all_pl_vars)
             for v in formula.inner_formula.free_variables()
             if v not in isla_to_pl_vars_mapping})

        inner_free_vars_to_lists_map: Dict[isla.Variable, pl.CompoundTerm] = {}

        lambda_goals.append(psc.pred("append",
                                     psc.list_term(in_var_path_var, qfd_var_rel_path_var),
                                     qfd_var_path_var))
        qfd_var_tree_var = self.fresh_variable("QfdVarTree", all_pl_vars)
        lambda_goals.append(psc.pred("get_subtree", in_var_tree_var, qfd_var_rel_path_var, qfd_var_tree_var))
        inner_free_vars_to_lists_map[formula.bound_variable] = psc.pair(qfd_var_path_var, qfd_var_tree_var)

        if formula.bind_expression is not None:
            for v in [v for v in formula.inner_formula.free_variables() if
                      v in formula.bind_expression.bound_variables()]:
                bv_rel_path_var = self.fresh_variable(f"{v.name}RelPath", all_pl_vars)
                lambda_goals.append(psc.pred("append",
                                             psc.list_term(qfd_var_rel_path_var,
                                                           python_list_to_prolog_list(rel_paths[v])),
                                             bv_rel_path_var))
                bv_tree_var = self.fresh_variable(f"{v.name}Tree", all_pl_vars)
                lambda_goals.append(psc.pred("get_subtree", in_var_tree_var, bv_rel_path_var, bv_tree_var))

                bv_path_var = self.fresh_variable(f"{v.name}Path", all_pl_vars)
                lambda_goals.append(psc.pred("append",
                                             psc.list_term(in_var_path_var, bv_rel_path_var),
                                             bv_path_var))
                inner_free_vars_to_lists_map[v] = psc.pair(bv_path_var, bv_tree_var)

        counter += 1
        child_head = pl.PredicateApplication(
            pl.Predicate(f"pred{counter}", len(formula.inner_formula.free_variables()) + 1),
            [inner_free_vars_to_lists_map.get(child_var, isla_to_pl_vars_mapping[child_var])
             for child_var in formula.inner_formula.free_variables()] +
            [out_var if type(formula) is isla.ForallFormula else result_var]
        )
        lambda_goals.append(child_head)

        if type(formula) is isla.ForallFormula:
            goals.append(psc.pred(
                "maplist",
                pl.LambdaTerm(
                    free_pl_vars.
                        union([in_var_path_var, in_var_tree_var]).
                        union([isla_to_pl_vars_mapping[v]
                               for v in formula.inner_formula.free_variables()
                               if formula.bind_expression is None
                               or v not in formula.bind_expression.bound_variables()]),
                    [qfd_var_rel_path_var, out_var],
                    lambda_goals,
                ),
                qfd_var_paths,
                sub_results
            ))

            goals.append(psc.pred("all", sub_results, result_var))
        elif type(formula) is isla.ExistsFormula:
            length_var = self.fresh_variable("L", all_pl_vars)
            goals.append(psc.pred("length", qfd_var_paths, length_var))
            idx_var = self.fresh_variable("I", all_pl_vars)
            goals.append(psc.infix_pred("in", idx_var, psc.infix_term("..", pl.Number(1), length_var)))
            goals.append(psc.pred("nth1", idx_var, qfd_var_paths, qfd_var_rel_path_var))
            goals.extend(lambda_goals)
        else:
            assert False

        child_rules, child_foreign_functions = self.translate_constraint(formula.inner_formula, counter)
        return [pl.Rule(head, goals)] + child_rules, child_foreign_functions

    def translate_propositional_combinator(self, formula: isla.PropositionalCombinator,
                                           counter: int) -> TranslationResult:
        head, isla_to_pl_vars_mapping, free_pl_vars, result_var, all_pl_vars = self.create_head(formula, counter)
        result_vars: List[pl.Variable] = []
        goals: List[pl.Goal] = []
        children_rules: List[pl.Rule] = []
        children_foreign_functions: List[ForeignFunctionSpec] = []

        for child_formula in formula.args:
            counter += 1
            child_result_var = self.fresh_variable(f"Result{counter}", all_pl_vars)
            result_vars.append(child_result_var)

            child_vars = [isla_to_pl_vars_mapping[v] for v in child_formula.free_variables()]
            child_head = pl.PredicateApplication(
                pl.Predicate(f"pred{counter}", len(child_vars) + 1),
                child_vars + [child_result_var]
            )

            goals.append(child_head)

            child_rules, child_foreign_functions = self.translate_constraint(child_formula, counter)
            children_rules.extend(child_rules)
            children_foreign_functions.extend(child_foreign_functions)

        child_result_vars_list = pl.ListTerm(result_vars)
        if type(formula) is isla.ConjunctiveFormula:
            goals.append(psc.pred("product", child_result_vars_list, result_var))
        elif type(formula) is isla.DisjunctiveFormula:
            sum_var = self.fresh_variable(f"Sum", all_pl_vars)
            goals.append(psc.pred("eqsum", child_result_vars_list, sum_var))
            goals.append(psc.clp_iff(psc.clp_eq(result_var, pl.Number(1)), psc.clp_gt(sum_var, pl.Number(0))))

        return [pl.Rule(head, goals)] + children_rules, children_foreign_functions

    def translate_predicate_formula(self, formula: isla.PredicateFormula, counter: int) -> TranslationResult:
        predicate = formula.predicate
        head, isla_to_pl_vars_mapping, free_pl_vars, result_var, all_pl_vars = self.create_head(formula, counter)

        if predicate is isla.BEFORE_PREDICATE:
            pvar1 = self.fresh_variable("Path1", all_pl_vars)
            pvar2 = self.fresh_variable("Path2", all_pl_vars.union([pvar1]))

            goals: List[pl.Goal] = [
                psc.unify(psc.pair(pvar1, psc.anon_var()), isla_to_pl_vars_mapping[formula.args[0]]),
                psc.unify(psc.pair(pvar2, psc.anon_var()), isla_to_pl_vars_mapping[formula.args[1]]),
                psc.ite(
                    pl.PredicateApplication(
                        pl.Predicate("path_is_before", 2), [pvar1, pvar2]
                    ),
                    psc.clp_eq(result_var, pl.Number(1)),
                    psc.clp_eq(result_var, pl.Number(0)),
                )
            ]

            return [pl.Rule(head, goals)], []
        else:
            def evaluate_predicate(success: int, list_of_pairs: List) -> bool:
                result = predicate.evaluate(*tuple(pyswip_output_to_python(atom) for atom in list_of_pairs))
                return result if success == 1 else not result

            vars_var = self.fresh_variable("Vars", all_pl_vars)
            goals: List[pl.Goal] = [
                psc.pred("term_variables", pl.ListTerm(free_pl_vars), vars_var),
                psc.pred("label", vars_var)
            ]

            free_pl_vars_list = psc.list_term(*free_pl_vars)
            free_pl_vars_paths_var = self.fresh_variable("Paths", all_pl_vars)
            free_pl_vars_trees_var = self.fresh_variable("Trees", all_pl_vars)
            concretized_trees_var = self.fresh_variable("Strings", all_pl_vars)
            concretized_args_var = self.fresh_variable("ConcrArgs", all_pl_vars)

            goals += [
                psc.pred("pairs_keys_values", free_pl_vars_list, free_pl_vars_paths_var, free_pl_vars_trees_var),
                psc.pred("maplist", pl.Atom("tree_to_string"), free_pl_vars_trees_var, concretized_trees_var),
                psc.pred("pairs_keys_values", concretized_args_var, free_pl_vars_paths_var, concretized_trees_var),
            ]

            function_name = f"evaluate_predicate_{counter}"
            eval_pred_appl_pos = psc.pred(function_name, pl.Number(1), concretized_args_var)
            eval_pred_appl_neg = psc.pred(function_name, pl.Number(0), concretized_args_var)
            goals.append(psc.disj(psc.conj(psc.clp_eq(result_var, pl.Number(1)), eval_pred_appl_pos),
                                  psc.conj(psc.clp_eq(result_var, pl.Number(0)), eval_pred_appl_neg)))

            return [pl.Rule(head, goals)], [(evaluate_predicate, function_name, 2)]

    def translate_smt_formula(self, formula: isla.SMTFormula, counter: int) -> TranslationResult:
        z3_formula: z3.BoolRef = formula.formula
        free_isla_vars: OrderedSet[isla.Variable] = formula.free_variables()
        head, isla_to_pl_vars_mapping, free_pl_vars, result_var, all_pl_vars = self.create_head(formula, counter)

        # TODO: This is still rather ad-hoc and fragile. Have to work on the SMT translation...
        if str(z3_formula.decl()) == "==" and all(is_z3_var(child) or z3.is_string_value(child)
                                                  for child in z3_formula.children()):
            tvar1 = self.fresh_variable("Tree1", all_pl_vars)
            tvar2 = self.fresh_variable("Tree2", all_pl_vars)

            vars_in_order = []
            indexes_of_nonvar_children = []
            for i, z3_child in enumerate(z3_formula.children()):
                if is_z3_var(z3_child):
                    vars_in_order.append(
                        isla_to_pl_vars_mapping[next(v for v in free_isla_vars if v.name == z3_child.as_string())])
                else:
                    indexes_of_nonvar_children.append(i)

            goals: List[pl.Goal]
            if len(free_isla_vars) == 2:
                goals = [
                    psc.unify(psc.pair(psc.anon_var(), tvar1), vars_in_order[0]),
                    psc.unify(psc.pair(psc.anon_var(), tvar2), vars_in_order[1]),
                    psc.pred("equal", tvar1, tvar2, result_var)
                ]
            else:
                assert len(free_isla_vars) == 1
                str_var = self.fresh_variable("Str", all_pl_vars)
                vars_var = self.fresh_variable("Vars", all_pl_vars)
                goals = [
                    psc.unify(psc.pair(psc.anon_var(), tvar1), vars_in_order[0]),
                    psc.pred("term_variables", psc.list_term(tvar1), vars_var),
                    psc.pred("label", vars_var),
                    psc.pred("tree_to_string", tvar1, str_var),
                    psc.disj(
                        psc.conj(
                            psc.clp_eq(result_var, pl.Number(1)),
                            psc.infix_pred(
                                "==",
                                str_var,
                                pl.StringTerm(z3_formula.children()[indexes_of_nonvar_children[0]].as_string())
                            )
                        ),
                        psc.conj(
                            psc.clp_eq(result_var, pl.Number(0)),
                            psc.infix_pred(
                                "\\=",
                                str_var,
                                pl.StringTerm(z3_formula.children()[indexes_of_nonvar_children[0]].as_string())
                            )
                        )
                    )
                ]

            return [pl.Rule(head, goals)], []
        else:
            def solve_smt(success: int, *atoms: bytes) -> bool:
                instantiation = z3.substitute(
                    z3_formula if success == 1 else z3.Not(z3_formula),
                    *tuple({z3.String(variable.name): z3.StringVal(pyswip_output_to_str(atom)[1:-1])
                            for variable, atom in zip(free_isla_vars, atoms)}.items()))

                z3.set_param("smt.string_solver", "z3str3")
                solver = z3.Solver()
                solver.add(instantiation)
                return solver.check() == z3.sat  # Set timeout?

            vars_var = self.fresh_variable("Vars", all_pl_vars)
            goals: List[pl.Goal] = [
                psc.pred("term_variables", pl.ListTerm(free_pl_vars), vars_var),
                psc.pred("label", vars_var)
            ]

            tvars: List[pl.Variable] = []
            strvars: List[pl.Variable] = []
            for variable in free_pl_vars:
                tvar = self.fresh_variable("Tree", free_pl_vars.union([result_var]).union(tvars).union(strvars))
                strvar = self.fresh_variable("StrTree", free_pl_vars.union([result_var]).union(tvars).union(strvars))
                tvars.append(tvar)
                strvars.append(strvar)
                goals.append(psc.unify(psc.pair(psc.anon_var(), tvar), variable))
                goals.append(psc.pred("tree_to_string", tvar, strvar))

            function_name = f"solve_smt_{counter}"
            smt_pred_appl_pos = psc.pred(function_name, pl.Number(1), *strvars)
            smt_pred_appl_neg = psc.pred(function_name, pl.Number(0), *strvars)
            goals.append(psc.disj(psc.conj(psc.clp_eq(result_var, pl.Number(1)), smt_pred_appl_pos),
                                  psc.conj(psc.clp_eq(result_var, pl.Number(0)), smt_pred_appl_neg)))

            return [pl.Rule(head, goals)], [(solve_smt, function_name, len(free_pl_vars) + 1)]

    def create_head(self, formula: isla.Formula, counter: int) -> \
            Tuple[
                pl.PredicateApplication,
                Dict[isla.Variable, pl.Variable],  # Mapping from isla to prolog variables
                OrderedSet[pl.Variable],  # Free prolog variables
                pl.Variable,  # The result variable
                OrderedSet[pl.Variable]  # Free prolog variables + result variable
            ]:
        free_isla_vars: OrderedSet[isla.Variable] = formula.free_variables()
        isla_to_pl_vars_mapping: List[Tuple[isla.Variable, pl.Variable]] = \
            [(isla_var, self.isla_to_prolog_var_map[isla_var]) for isla_var in free_isla_vars]
        free_pl_vars = OrderedSet([pl_variable for _, pl_variable in isla_to_pl_vars_mapping])
        all_pl_vars: OrderedSet[pl.Variable] = OrderedSet(free_pl_vars)

        result_var = self.fresh_variable("Result", free_pl_vars, add=False)
        all_pl_vars.add(result_var)

        head = pl.PredicateApplication(
            pl.Predicate(f"pred{counter}", len(all_pl_vars)),
            all_pl_vars
        )

        return head, dict(isla_to_pl_vars_mapping), free_pl_vars, result_var, all_pl_vars

    def fresh_variable(self, name_pattern: str, context_vars: OrderedSet[pl.Variable], add=True) -> pl.Variable:
        result = self.to_prolog_var(name_pattern)
        i = 0
        while result in context_vars:
            name = f"{name_pattern}_{i}"
            result = self.to_prolog_var(name)
            i += 1

        if add:
            context_vars.add(result)

        return result

    def to_prolog_var(self, variable: Union[str, isla.Variable]) -> pl.Variable:
        result = variable if type(variable) is str else variable.name
        result = re.sub('[^_a-zA-Z0-9]', '', result)

        if result[0] != "_" and not result[0].isupper():
            if result[0].isalpha():
                result = result[0].upper() + result[1:]
            else:
                result = "_" + result

        return pl.Variable(result)

    def translate_grammar(self) -> List[pl.Rule]:
        # TODO XXX: Have to output base cases first, otherwise leads to non-termination!
        # TODO XXX: Also, recursive calls have to come first.
        # TODO XXX: Have to think about string representation! Should use flat lists... Maybe even with
        #           symbolic ints representing char points.

        """
        Translates a grammar to Prolog.

        :param grammar: The grammar in canonical form.
        :param predicate_map: Mapping of nonterminal names (w/o the "<", ">") to the corresponding predicate names.
        Accounts for predefined predicates.
        :param numeric_nonterminals: Nonterminals of integer type, mapped to their bounds.
        :param atomic_string_nonterminals: Nonterminals of string type whose internal structure does not matter for
        the constraint at hand, and which therefore can be abstracted by integers (for later fuzzing).
        :return: The prolog translation of `grammar`.
        """
        rules: List[pl.Rule] = []
        graph = GrammarGraph.from_grammar(non_canonical(self.grammar))

        for nonterminal, alternatives in [(n, a) for n, a in self.grammar.items()
                                          if n not in self.numeric_nonterminals
                                             and n not in self.atomic_string_nonterminals]:
            nonterminal = nonterminal[1:-1]
            for alternative in alternatives:
                params: List[pl.Term] = []
                variables: Dict[str, str] = {}
                for symbol in alternative:
                    if is_nonterminal(symbol):
                        symbol_type = symbol[1:-1]
                        var_name = symbol_type.capitalize()
                        i = 1
                        while var_name in variables:
                            var_name += f"_{i}"
                        variables[var_name] = symbol_type
                        params.append(pl.Variable(var_name))
                    else:
                        params.append(pl.ListTerm([pl.StringTerm(symbol), pl.ListTerm([])]))

                atom_name = self.predicate_map[nonterminal]
                head = pl.PredicateApplication(
                    pl.Predicate(atom_name, 1),
                    [pl.ListTerm([pl.Atom(atom_name), pl.ListTerm(params)])])

                goals = []
                if variables:
                    # Need to call recursive nonterminals first
                    variables_list = sorted(variables.keys(),
                                            key=lambda n: (
                                                node := graph.get_node(f"<{variables[n]}>"),
                                                chr(0) if node.reachable(node) else n[1:-1])[-1])

                    goals = [pl.PredicateApplication(pl.Predicate(self.predicate_map[variables[variable]], 1),
                                                     [pl.Variable(variable)])
                             for variable in variables_list]

                rules.append(pl.Rule(head, goals))

        for nonterminal in self.numeric_nonterminals:
            nonterminal_name = nonterminal[1:-1]
            c = pl.Variable("C")
            leq = pl.Predicate("#=<", 2, infix=True)

            rules.append(pl.Rule(pl.PredicateApplication(
                pl.Predicate(self.predicate_map[nonterminal_name], 1),
                [pl.ListTerm([pl.Atom(nonterminal_name), pl.ListTerm([pl.ListTerm([c, pl.ListTerm([])])])])]
            ), [
                pl.PredicateApplication(leq, [pl.Number(self.numeric_nonterminals[nonterminal][0]), c]),
                pl.PredicateApplication(leq, [c, pl.Number(self.numeric_nonterminals[nonterminal][1])])
            ]))

        for nonterminal in self.atomic_string_nonterminals:
            nonterminal_name = nonterminal[1:-1]
            c = pl.Variable("C")
            leq = pl.Predicate("#=<", 2, infix=True)

            rules.append(pl.Rule(pl.PredicateApplication(
                pl.Predicate(self.predicate_map[nonterminal_name], 1),
                [pl.ListTerm([pl.Atom(nonterminal_name), pl.ListTerm([pl.ListTerm([c, pl.ListTerm([])])])])]
            ), [
                pl.PredicateApplication(leq, [pl.Number(0), c]),
                pl.PredicateApplication(leq, [c, pl.Number(self.atomic_string_nonterminals[nonterminal])])
            ]))

        # % Alternative for using foreign method fuzz function: Embed into Prolog code. Speed difference
        # % seems to be negligible.
        # for nonterminal in self.atomic_string_nonterminals:
        #     grammar = GrammarGraph.from_grammar(non_canonical(self.grammar)).subgraph(nonterminal).to_grammar()
        #     fuzzer = GrammarCoverageFuzzer(grammar)
        #
        #     for i in range(self.atomic_string_nonterminals[nonterminal]):
        #         rules.append(pl.Rule(psc.pred("fuzz",
        #                                       pl.Atom(nonterminal[1:-1]),
        #                                       pl.Number(i),
        #                                       pl.StringTerm(fuzzer.fuzz())), []))

        return rules

    def compute_atomic_string_nonterminals(self) -> Dict[str, int]:
        # TODO: We should not consider as atomic nonterminals with a simple domain, e.g., a brief enumeration.
        # TODO: It also makes sense to constrain the numeric domains of atomic nonterminals if there are
        #       fewer options available than the given maximum domain element.
        assert hasattr(self, "numeric_nonterminals")

        def reachable(nonterminal: str) -> Set[str]:
            graph = GrammarGraph.from_grammar(non_canonical(self.grammar))
            dist = graph.dijkstra(graph.get_node(nonterminal))[0]
            return set([node.symbol for node in dist.keys()
                        if dist[node] < sys.maxsize
                        and node.symbol != nonterminal
                        and type(node) is NonterminalNode])

        used_nonterminals = OrderedSet([variable.n_type
                                        for variable in self.used_variables
                                        if is_nonterminal(variable.n_type)])

        non_atomic_nonterminals = OrderedSet([])

        # Only consider nonterminals that don't reach other used nonterminals
        used_proxy_nonterminals: OrderedSet[str] = OrderedSet([
            used_nonterminal
            for used_nonterminal in
            used_nonterminals.difference(set(self.numeric_nonterminals.keys()))
            if reachable(used_nonterminal).intersection(used_nonterminals)
        ])

        non_atomic_nonterminals |= used_proxy_nonterminals

        unused_sink_nonterminals: OrderedSet[str] = OrderedSet([
            unused_nonterminal
            for unused_nonterminal in
            OrderedSet(self.grammar.keys())
                .difference(used_nonterminals)
                .difference(set(self.numeric_nonterminals.keys()))
            if not reachable(unused_nonterminal).intersection(used_nonterminals)
        ])

        non_atomic_variables = OrderedSet([])

        class NonAtomicVisitor(isla.FormulaVisitor):
            def visit_forall_formula(self, formula: isla.ForallFormula):
                non_atomic_variables.add(formula.in_variable)
                if formula.bind_expression is not None:
                    non_atomic_variables.add(formula.bound_variable)

            def visit_exists_formula(self, formula: isla.ExistsFormula):
                non_atomic_variables.add(formula.in_variable)
                if formula.bind_expression is not None:
                    non_atomic_variables.add(formula.bound_variable)

            def visit_predicate_formula(self, formula: isla.PredicateFormula):
                if formula.predicate != isla.BEFORE_PREDICATE:
                    non_atomic_variables.update(formula.free_variables())

            def visit_smt_formula(self, formula: isla.SMTFormula):
                # TODO: This is still quite arbitrary and ad-hoc, should be fundamentally investigated and reworked.

                if str(formula.formula.decl()) == "==" and any(not is_z3_var(child)
                                                               for child in formula.formula.children()):
                    # In equations like "Var == 'asdf'", it slows down the process to first fuzz Var.
                    # Thus, we have to leave Var's type concrete.
                    non_atomic_variables.update(formula.free_variables())

                for expr in visit_z3_expr(formula.formula):
                    # Any non-trivial string expression, e.g., substring computations
                    # or regex operations, exclude the involved variables from atomic
                    # representations, since their internal structure matters.
                    # TODO: Have to more thoroughly test whether this suffices.
                    if expr.decl().name() == "str.in_re":
                        non_atomic_variables.update(formula.free_variables())

                    if z3.is_string(expr) and not z3.is_string_value(expr) and not z3.is_const(expr):
                        non_atomic_variables.update(formula.free_variables())

        self.formula.accept(NonAtomicVisitor())

        non_atomic_nonterminals |= [variable.n_type for variable in non_atomic_variables]

        return {nonterminal: Translator.FUZZING_DEPTH_ATOMIC_STRING_NONTERMINALS
                for nonterminal in used_nonterminals
                    .difference(non_atomic_nonterminals)
                    .union(unused_sink_nonterminals)}

    def compute_numeric_nonterminals(self) -> Dict[str, Tuple[int, int]]:
        # TODO: This could be a performance bottleneck. We should try to statically solve this!
        result = {}
        noncanonical = non_canonical(self.grammar)
        for nonterminal in self.grammar:
            fuzzer = GrammarCoverageFuzzer(GrammarGraph.from_grammar(noncanonical).subgraph(nonterminal).to_grammar())
            lower_bound, upper_bound = sys.maxsize, -1
            for _ in range(100):
                inp = fuzzer.fuzz()
                if not (inp.isnumeric()):
                    break
                int_repr = int(inp)
                if int_repr < lower_bound:
                    lower_bound = int_repr
                elif int_repr > upper_bound:
                    upper_bound = int_repr
            else:
                result[nonterminal] = (lower_bound, upper_bound)

        return result

    def compute_predicate_names_for_nonterminals(self) -> Dict[str, str]:
        """
        Creates a mapping of nonterminal names (w/o the "<", ">") to the corresponding predicate names. Accounts
        for predefined predicates.

        :return: The mapping.
        """
        prolog = Prolog()
        predicate_map: Dict[str, str] = {}
        for nonterminal in self.grammar:
            nonterminal = nonterminal[1:-1]
            idx = 0
            curr_name = nonterminal.lower()
            while self.predicate_defined(curr_name, 1):
                curr_name = f"{nonterminal.lower()}_{idx}"
                idx += 1

            predicate_map[nonterminal] = curr_name

        return predicate_map

    def predicate_defined(self, name: str, arity: int):
        prolog = Prolog()
        return len(list(prolog.query(f"current_predicate({name}/{arity})"))) > 0
