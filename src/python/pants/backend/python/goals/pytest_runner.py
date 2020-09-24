# Copyright 2018 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import itertools
import logging
from dataclasses import dataclass
from pathlib import PurePath
from typing import Optional, Tuple

from pants.backend.python.goals.coverage_py import (
    CoverageConfig,
    CoverageSubsystem,
    PytestCoverageData,
)
from pants.backend.python.subsystems.pytest import PyTest
from pants.backend.python.target_types import (
    PythonInterpreterCompatibility,
    PythonRuntimeBinaryDependencies,
    PythonTestsSources,
    PythonTestsTimeout,
)
from pants.backend.python.util_rules.pex import (
    Pex,
    PexInterpreterConstraints,
    PexProcess,
    PexRequest,
    PexRequirements,
)
from pants.backend.python.util_rules.pex_from_targets import PexFromTargetsRequest
from pants.backend.python.util_rules.python_sources import (
    PythonSourceFiles,
    PythonSourceFilesRequest,
)
from pants.core.goals.binary import BinaryFieldSet, CreatedBinary
from pants.core.goals.test import (
    TestDebugRequest,
    TestExtraEnv,
    TestFieldSet,
    TestResult,
    TestSubsystem,
)
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.engine.addresses import Address, Addresses, AddressInput
from pants.engine.fs import AddPrefix, Digest, DigestSubset, MergeDigests, PathGlobs, Snapshot
from pants.engine.process import FallibleProcessResult, InteractiveProcess, Process
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import (
    FieldSetsPerTarget,
    FieldSetsPerTargetRequest,
    Targets,
    TransitiveTargets,
)
from pants.engine.unions import UnionRule
from pants.option.global_options import GlobalOptions
from pants.python.python_setup import PythonSetup
from pants.util.logging import LogLevel

logger = logging.getLogger()


@dataclass(frozen=True)
class PythonTestFieldSet(TestFieldSet):
    required_fields = (PythonTestsSources,)

    sources: PythonTestsSources
    timeout: PythonTestsTimeout
    runtime_binary_dependencies: PythonRuntimeBinaryDependencies

    def is_conftest(self) -> bool:
        """We skip `conftest.py`, even though it belongs to a `python_tests` target, because it does
        not have any tests to run on."""
        return (
            not self.address.is_base_target
            and PurePath(self.address.filename).name == "conftest.py"
        )


@dataclass(frozen=True)
class TestSetupRequest:
    field_set: PythonTestFieldSet
    is_debug: bool


@dataclass(frozen=True)
class TestSetup:
    process: Process
    results_file_name: Optional[str]

    # Prevent this class from being detected by pytest as a test class.
    __test__ = False


@rule(level=LogLevel.DEBUG)
async def setup_pytest_for_target(
    request: TestSetupRequest,
    pytest: PyTest,
    test_subsystem: TestSubsystem,
    python_setup: PythonSetup,
    coverage_config: CoverageConfig,
    coverage_subsystem: CoverageSubsystem,
    test_extra_env: TestExtraEnv,
    global_options: GlobalOptions,
) -> TestSetup:
    test_addresses = Addresses((request.field_set.address,))

    transitive_targets = await Get(TransitiveTargets, Addresses, test_addresses)
    all_targets = transitive_targets.closure

    interpreter_constraints = PexInterpreterConstraints.create_from_compatibility_fields(
        (
            tgt[PythonInterpreterCompatibility]
            for tgt in all_targets
            if tgt.has_field(PythonInterpreterCompatibility)
        ),
        python_setup,
    )

    # Defaults to zip_safe=False.
    requirements_pex_request = Get(
        Pex,
        PexFromTargetsRequest,
        PexFromTargetsRequest.for_requirements(test_addresses, internal_only=True),
    )

    pytest_pex_request = Get(
        Pex,
        PexRequest(
            output_filename="pytest.pex",
            requirements=PexRequirements(pytest.get_requirement_strings()),
            interpreter_constraints=interpreter_constraints,
            entry_point="pytest:main",
            internal_only=True,
            additional_args=(
                # NB: We set `--not-zip-safe` because Pytest plugin discovery, which uses
                # `importlib_metadata` and thus `zipp`, does not play nicely when doing import
                # magic directly from zip files. `zipp` has pathologically bad behavior with large
                # zipfiles.
                # TODO: this does have a performance cost as the pex must now be expanded to disk.
                # Long term, it would be better to fix Zipp (whose fix would then need to be used
                # by importlib_metadata and then by Pytest). See
                # https://github.com/jaraco/zipp/pull/26.
                "--not-zip-safe",
                # TODO(John Sirois): Support shading python binaries:
                #   https://github.com/pantsbuild/pants/issues/9206
                "--pex-path",
                requirements_pex_request.input.output_filename,
            ),
        ),
    )

    prepared_sources_request = Get(
        PythonSourceFiles, PythonSourceFilesRequest(all_targets, include_files=True)
    )

    # Create any binaries that the test depends on through the `runtime_binary_dependencies` field.
    binaries: Tuple[CreatedBinary, ...] = ()
    if request.field_set.runtime_binary_dependencies.value:
        runtime_binary_addresses = await MultiGet(
            Get(
                Address,
                AddressInput,
                AddressInput.parse(v, relative_to=request.field_set.address.spec_path),
            )
            for v in request.field_set.runtime_binary_dependencies.value
        )
        runtime_binary_targets = await Get(Targets, Addresses(runtime_binary_addresses))
        field_sets_per_target = await Get(
            FieldSetsPerTarget,
            FieldSetsPerTargetRequest(BinaryFieldSet, runtime_binary_targets),
        )
        binaries = await MultiGet(
            Get(CreatedBinary, BinaryFieldSet, field_set)
            for field_set in field_sets_per_target.field_sets
        )

    # Get the file names for the test_target so that we can specify to Pytest precisely which files
    # to test, rather than using auto-discovery.
    field_set_source_files_request = Get(
        SourceFiles, SourceFilesRequest([request.field_set.sources])
    )

    pytest_pex, requirements_pex, prepared_sources, field_set_source_files = await MultiGet(
        pytest_pex_request,
        requirements_pex_request,
        prepared_sources_request,
        field_set_source_files_request,
    )

    input_digest = await Get(
        Digest,
        MergeDigests(
            (
                coverage_config.digest,
                prepared_sources.source_files.snapshot.digest,
                requirements_pex.digest,
                pytest_pex.digest,
                *(binary.digest for binary in binaries),
            )
        ),
    )

    add_opts = [f"--color={'yes' if global_options.options.colors else 'no'}"]
    output_files = []

    results_file_name = None
    if pytest.options.junit_xml_dir and not request.is_debug:
        results_file_name = f"{request.field_set.address.path_safe_spec}.xml"
        add_opts.extend(
            (f"--junitxml={results_file_name}", "-o", f"junit_family={pytest.options.junit_family}")
        )
        output_files.append(results_file_name)

    coverage_args = []
    if test_subsystem.use_coverage and not request.is_debug:
        output_files.append(".coverage")
        cov_paths = coverage_subsystem.filter if coverage_subsystem.filter else (".",)
        coverage_args = [
            "--cov-report=",  # Turn off output.
            *itertools.chain.from_iterable(["--cov", cov_path] for cov_path in cov_paths),
        ]

    extra_env = {
        "PYTEST_ADDOPTS": " ".join(add_opts),
        "PEX_EXTRA_SYS_PATH": ":".join(prepared_sources.source_roots),
    }

    extra_env.update(test_extra_env.env)

    process = await Get(
        Process,
        PexProcess(
            pytest_pex,
            argv=(*pytest.options.args, *coverage_args, *field_set_source_files.files),
            extra_env=extra_env,
            input_digest=input_digest,
            output_files=output_files,
            timeout_seconds=request.field_set.timeout.calculate_from_global_options(pytest),
            execution_slot_variable=pytest.options.execution_slot_var,
            description=f"Run Pytest for {request.field_set.address}",
            level=LogLevel.DEBUG,
            uncacheable=test_subsystem.force and not request.is_debug,
        ),
    )
    return TestSetup(process, results_file_name=results_file_name)


@rule(desc="Run Pytest", level=LogLevel.DEBUG)
async def run_python_test(
    field_set: PythonTestFieldSet, test_subsystem: TestSubsystem, pytest: PyTest
) -> TestResult:
    if field_set.is_conftest():
        return TestResult.skip(field_set.address)

    setup = await Get(TestSetup, TestSetupRequest(field_set, is_debug=False))
    result = await Get(FallibleProcessResult, Process, setup.process)

    coverage_data = None
    if test_subsystem.use_coverage:
        coverage_snapshot = await Get(
            Snapshot, DigestSubset(result.output_digest, PathGlobs([".coverage"]))
        )
        if coverage_snapshot.files == (".coverage",):
            coverage_data = PytestCoverageData(field_set.address, coverage_snapshot.digest)
        else:
            logger.warning(f"Failed to generate coverage data for {field_set.address}.")

    xml_results_snapshot = None
    if setup.results_file_name:
        xml_results_snapshot = await Get(
            Snapshot, DigestSubset(result.output_digest, PathGlobs([setup.results_file_name]))
        )
        if xml_results_snapshot.files == (setup.results_file_name,):
            xml_results_snapshot = await Get(
                Snapshot,
                AddPrefix(xml_results_snapshot.digest, pytest.options.junit_xml_dir),
            )
        else:
            logger.warning(f"Failed to generate JUnit XML data for {field_set.address}.")

    return TestResult.from_fallible_process_result(
        result,
        address=field_set.address,
        coverage_data=coverage_data,
        xml_results=xml_results_snapshot,
    )


@rule(desc="Set up Pytest to run interactively", level=LogLevel.DEBUG)
async def debug_python_test(field_set: PythonTestFieldSet) -> TestDebugRequest:
    if field_set.is_conftest():
        return TestDebugRequest(None)
    setup = await Get(TestSetup, TestSetupRequest(field_set, is_debug=True))
    return TestDebugRequest(
        InteractiveProcess.from_process(setup.process, forward_signals_to_process=False)
    )


def rules():
    return [*collect_rules(), UnionRule(TestFieldSet, PythonTestFieldSet)]
