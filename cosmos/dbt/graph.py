from __future__ import annotations

import base64
import datetime
import functools
import itertools
import json
import os
import platform
import tempfile
import zlib
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Any

from airflow.models import Variable

from cosmos import cache, settings
from cosmos.config import ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.constants import (
    DBT_LOG_DIR_NAME,
    DBT_LOG_FILENAME,
    DBT_LOG_PATH_ENVVAR,
    DBT_TARGET_DIR_NAME,
    DBT_TARGET_PATH_ENVVAR,
    DbtResourceType,
    ExecutionMode,
    LoadMode,
)
from cosmos.dbt.parser.project import LegacyDbtProject
from cosmos.dbt.project import create_symlinks, environ, get_partial_parse_path, has_non_empty_dependencies_file
from cosmos.dbt.selector import select_nodes
from cosmos.log import get_logger

logger = get_logger(__name__)


class CosmosLoadDbtException(Exception):
    """
    Exception raised while trying to load a `dbt` project as a `DbtGraph` instance.
    """

    pass


@dataclass
class DbtNode:
    """
    Metadata related to a dbt node (e.g. model, seed, snapshot).
    """

    unique_id: str
    resource_type: DbtResourceType
    depends_on: list[str]
    file_path: Path
    tags: list[str] = field(default_factory=lambda: [])
    config: dict[str, Any] = field(default_factory=lambda: {})
    has_test: bool = False

    @property
    def resource_name(self) -> str:
        """
        Use this property to retrieve the resource name for command generation, for instance: ["dbt", "run", "--models", f"{resource_name}"].
        The unique_id format is defined as [<resource_type>.<package>.<resource_name>](https://docs.getdbt.com/reference/artifacts/manifest-json#resource-details).
        For a special case like a versioned model, the unique_id follows this pattern: [model.<package>.<resource_name>.<version>](https://github.com/dbt-labs/dbt-core/blob/main/core/dbt/contracts/graph/node_args.py#L26C3-L31)
        """
        return self.unique_id.split(".", 2)[2]

    @property
    def name(self) -> str:
        """
        Use this property as the task name or task group name.
        Replace period (.) with underscore (_) due to versioned models.
        """
        return self.resource_name.replace(".", "_")

    @property
    def context_dict(self) -> dict[str, Any]:
        """
        Returns a dictionary containing all the attributes of the DbtNode object,
        ensuring that the output is JSON serializable so it can be stored in Airflow's db
        """
        return {
            "unique_id": self.unique_id,
            "resource_type": self.resource_type.value,  # convert enum to value
            "depends_on": self.depends_on,
            "file_path": str(self.file_path),  # convert path to string
            "tags": self.tags,
            "config": self.config,
            "has_test": self.has_test,
            "resource_name": self.resource_name,
            "name": self.name,
        }


def run_command(command: list[str], tmp_dir: Path, env_vars: dict[str, str]) -> str:
    """Run a command in a subprocess, returning the stdout."""
    logger.info("Running command: `%s`", " ".join(command))
    logger.debug("Environment variable keys: %s", env_vars.keys())
    process = Popen(
        command,
        stdout=PIPE,
        stderr=PIPE,
        cwd=tmp_dir,
        universal_newlines=True,
        env=env_vars,
    )
    stdout, stderr = process.communicate()
    returncode = process.returncode

    if 'Run "dbt deps" to install package dependencies' in stdout and command[1] == "ls":
        raise CosmosLoadDbtException(
            "Unable to run dbt ls command due to missing dbt_packages. Set RenderConfig.dbt_deps=True."
        )

    if returncode or "Error" in stdout.replace("WarnErrorOptions", ""):
        details = stderr or stdout
        raise CosmosLoadDbtException(f"Unable to run {command} due to the error:\n{details}")

    return stdout


def parse_dbt_ls_output(project_path: Path | None, ls_stdout: str) -> dict[str, DbtNode]:
    """Parses the output of `dbt ls` into a dictionary of `DbtNode` instances."""
    nodes = {}
    for line in ls_stdout.split("\n"):
        try:
            node_dict = json.loads(line.strip())
        except json.decoder.JSONDecodeError:
            logger.debug("Skipped dbt ls line: %s", line)
        else:
            node = DbtNode(
                unique_id=node_dict["unique_id"],
                resource_type=DbtResourceType(node_dict["resource_type"]),
                depends_on=node_dict.get("depends_on", {}).get("nodes", []),
                file_path=project_path / node_dict["original_file_path"],
                tags=node_dict.get("tags", []),
                config=node_dict.get("config", {}),
            )
            nodes[node.unique_id] = node
            logger.debug("Parsed dbt resource `%s` of type `%s`", node.unique_id, node.resource_type)
    return nodes


class DbtGraph:
    """
    A dbt project graph (represented by `nodes` and `filtered_nodes`).
    Supports different ways of loading the `dbt` project into this representation.

    Different loading methods can result in different `nodes` and `filtered_nodes`.
    """

    nodes: dict[str, DbtNode] = dict()
    filtered_nodes: dict[str, DbtNode] = dict()
    load_method: LoadMode = LoadMode.AUTOMATIC
    current_version: str = ""

    def __init__(
        self,
        project: ProjectConfig,
        render_config: RenderConfig = RenderConfig(),
        execution_config: ExecutionConfig = ExecutionConfig(),
        profile_config: ProfileConfig | None = None,
        cache_dir: Path | None = None,
        cache_identifier: str = "",
        dbt_vars: dict[str, str] | None = None,
        airflow_metadata: dict[str, str] | None = None,
    ):
        self.project = project
        self.render_config = render_config
        self.profile_config = profile_config
        self.execution_config = execution_config
        self.cache_dir = cache_dir
        self.airflow_metadata = airflow_metadata or {}
        if cache_identifier:
            self.dbt_ls_cache_key = cache.create_cache_key(cache_identifier)
        else:
            self.dbt_ls_cache_key = ""
        self.dbt_vars = dbt_vars or {}

    @cached_property
    def env_vars(self) -> dict[str, str]:
        """
        User-defined environment variables, relevant to running dbt ls.
        """
        return self.render_config.env_vars or self.project.env_vars or {}

    @cached_property
    def project_path(self) -> Path:
        """
        Return the user-defined path to their dbt project. Tries to retrieve the configuration from render_config and
        (legacy support) ExecutionConfig, where it was originally defined.
        """
        # we're considering the execution_config only due to backwards compatibility
        path = self.render_config.project_path or self.project.dbt_project_path or self.execution_config.project_path
        if not path:
            raise CosmosLoadDbtException(
                "Unable to load project via dbt ls without RenderConfig.dbt_project_path, ProjectConfig.dbt_project_path or ExecutionConfig.dbt_project_path"
            )
        return path.absolute()

    @cached_property
    def dbt_ls_args(self) -> list[str]:
        """
        Flags set while running dbt ls. This information is also used to define the dbt ls cache key.
        """
        ls_args = []
        if self.render_config.exclude:
            ls_args.extend(["--exclude", *self.render_config.exclude])

        if self.render_config.select:
            ls_args.extend(["--select", *self.render_config.select])

        if self.project.dbt_vars:
            ls_args.extend(["--vars", json.dumps(self.project.dbt_vars, sort_keys=True)])

        if self.render_config.selector:
            ls_args.extend(["--selector", self.render_config.selector])

        if not self.project.partial_parse:
            ls_args.append("--no-partial-parse")

        return ls_args

    @cached_property
    def dbt_ls_cache_key_args(self) -> list[str]:
        """
        Values that are used to represent the dbt ls cache key. If any parts are changed, the dbt ls command will be
        executed and the new value will be stored.
        """
        # if dbt deps, we can consider the md5 of the packages or deps file
        cache_args = list(self.dbt_ls_args)
        env_vars = self.env_vars
        if env_vars:
            envvars_str = json.dumps(env_vars, sort_keys=True)
            cache_args.append(envvars_str)
        if self.render_config.airflow_vars_to_purge_dbt_ls_cache:
            for var_name in self.render_config.airflow_vars_to_purge_dbt_ls_cache:
                airflow_vars = [var_name, Variable.get(var_name, "")]
                cache_args.extend(airflow_vars)

        logger.debug(f"Value of `dbt_ls_cache_key_args` for <{self.dbt_ls_cache_key}>: {cache_args}")
        return cache_args

    def save_dbt_ls_cache(self, dbt_ls_output: str) -> None:
        """
        Store compressed dbt ls output into an Airflow Variable.

        Stores:
        {
            "version": "cache-version",
            "dbt_ls_compressed": "compressed dbt ls output",
            "last_modified": "Isoformat timestamp"
        }
        """
        # This compression reduces the dbt ls output to 10% of the original size
        compressed_data = zlib.compress(dbt_ls_output.encode("utf-8"))
        encoded_data = base64.b64encode(compressed_data)
        dbt_ls_compressed = encoded_data.decode("utf-8")
        cache_dict = {
            "version": cache._calculate_dbt_ls_cache_current_version(
                self.dbt_ls_cache_key, self.project_path, self.dbt_ls_cache_key_args
            ),
            "dbt_ls_compressed": dbt_ls_compressed,
            "last_modified": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            **self.airflow_metadata,
        }
        Variable.set(self.dbt_ls_cache_key, cache_dict, serialize_json=True)

    def get_dbt_ls_cache(self) -> dict[str, str]:
        """
        Retrieve previously saved dbt ls cache from an Airflow Variable, decompressing the dbt ls output.

        Outputs:
        {
            "version": "cache-version",
            "dbt_ls": "uncompressed dbt ls output",
            "last_modified": "Isoformat timestamp"
        }
        """
        cache_dict: dict[str, str] = {}
        try:
            cache_dict = Variable.get(self.dbt_ls_cache_key, deserialize_json=True)
        except (json.decoder.JSONDecodeError, KeyError):
            return cache_dict
        else:
            dbt_ls_compressed = cache_dict.pop("dbt_ls_compressed", None)
            if dbt_ls_compressed:
                encoded_data = base64.b64decode(dbt_ls_compressed.encode())
                cache_dict["dbt_ls"] = zlib.decompress(encoded_data).decode()

        return cache_dict

    def load(
        self,
        method: LoadMode = LoadMode.AUTOMATIC,
        execution_mode: ExecutionMode = ExecutionMode.LOCAL,
    ) -> None:
        """
        Load a `dbt` project into a `DbtGraph`, setting `nodes` and `filtered_nodes` accordingly.

        :param method: How to load `nodes` from a `dbt` project (automatically, using custom parser, using dbt manifest
            or dbt ls)
        :param execution_mode: Where Cosmos should run each dbt task (e.g. ExecutionMode.KUBERNETES)

        Fundamentally, there are two different execution paths
        There is automatic, and manual.
        """
        load_method = {
            LoadMode.CUSTOM: self.load_via_custom_parser,
            LoadMode.DBT_LS: self.load_via_dbt_ls,
            LoadMode.DBT_LS_FILE: self.load_via_dbt_ls_file,
            LoadMode.DBT_LS_CACHE: self.load_via_dbt_ls_cache,
            LoadMode.DBT_MANIFEST: self.load_from_dbt_manifest,
        }

        if method == LoadMode.AUTOMATIC:
            if self.project.is_manifest_available():
                self.load_from_dbt_manifest()
            else:
                if execution_mode == ExecutionMode.LOCAL and self.profile_config:
                    try:
                        self.load_via_dbt_ls()
                    except FileNotFoundError:
                        self.load_via_custom_parser()
                else:
                    self.load_via_custom_parser()
        else:
            load_method[method]()

        self.update_node_dependency()

        logger.info("Total nodes: %i", len(self.nodes))
        logger.info("Total filtered nodes: %i", len(self.nodes))

    def run_dbt_ls(
        self, dbt_cmd: str, project_path: Path, tmp_dir: Path, env_vars: dict[str, str]
    ) -> dict[str, DbtNode]:
        """Runs dbt ls command and returns the parsed nodes."""
        ls_command = [dbt_cmd, "ls", "--output", "json"]

        ls_args = self.dbt_ls_args
        ls_command.extend(self.local_flags)
        ls_command.extend(ls_args)

        stdout = run_command(ls_command, tmp_dir, env_vars)

        logger.debug("dbt ls output: %s", stdout)
        log_filepath = self.log_dir / DBT_LOG_FILENAME
        logger.debug("dbt logs available in: %s", log_filepath)
        if log_filepath.exists():
            with open(log_filepath) as logfile:
                for line in logfile:
                    logger.debug(line.strip())

        if self.should_use_dbt_ls_cache():
            self.save_dbt_ls_cache(stdout)

        nodes = parse_dbt_ls_output(project_path, stdout)
        return nodes

    def load_via_dbt_ls(self) -> None:
        """Retrieve the dbt ls cache if enabled and available or run dbt ls"""
        if not self.load_via_dbt_ls_cache():
            self.load_via_dbt_ls_without_cache()

    @functools.lru_cache
    def should_use_dbt_ls_cache(self) -> bool:
        """Identify if Cosmos should use/store dbt ls cache or not."""
        return settings.enable_cache and settings.enable_cache_dbt_ls and bool(self.dbt_ls_cache_key)

    def load_via_dbt_ls_cache(self) -> bool:
        """(Try to) load dbt ls cache from an Airflow Variable"""

        logger.info(f"Trying to parse the dbt project using dbt ls cache {self.dbt_ls_cache_key}...")
        if self.should_use_dbt_ls_cache():
            project_path = self.project_path

            cache_dict = self.get_dbt_ls_cache()
            if not cache_dict:
                logger.info(f"Cosmos performance: Cache miss for {self.dbt_ls_cache_key}")
                return False

            cache_version = cache_dict.get("version")
            dbt_ls_cache = cache_dict.get("dbt_ls")

            current_version = cache._calculate_dbt_ls_cache_current_version(
                self.dbt_ls_cache_key, project_path, self.dbt_ls_cache_key_args
            )

            if dbt_ls_cache and not cache.was_project_modified(cache_version, current_version):
                logger.info(
                    f"Cosmos performance [{platform.node()}|{os.getpid()}]: The cache size for {self.dbt_ls_cache_key} is {len(dbt_ls_cache)}"
                )
                self.load_method = LoadMode.DBT_LS_CACHE

                nodes = parse_dbt_ls_output(project_path=project_path, ls_stdout=dbt_ls_cache)
                self.nodes = nodes
                self.filtered_nodes = nodes
                logger.info(f"Cosmos performance: Cache hit for {self.dbt_ls_cache_key} - {current_version}")
                return True
        logger.info(f"Cosmos performance: Cache miss for {self.dbt_ls_cache_key} - skipped")
        return False

    def load_via_dbt_ls_without_cache(self) -> None:
        """
        This is the most accurate way of loading `dbt` projects and filtering them out, since it uses the `dbt` command
        line for both parsing and filtering the nodes.

        Updates in-place:
        * self.nodes
        * self.filtered_nodes
        """
        self.load_method = LoadMode.DBT_LS
        self.render_config.validate_dbt_command(fallback_cmd=self.execution_config.dbt_executable_path)
        dbt_cmd = self.render_config.dbt_executable_path
        dbt_cmd = dbt_cmd.as_posix() if isinstance(dbt_cmd, Path) else dbt_cmd

        logger.info(f"Trying to parse the dbt project in `{self.render_config.project_path}` using dbt ls...")
        project_path = self.project_path
        if not self.profile_config:
            raise CosmosLoadDbtException("Unable to load project via dbt ls without a profile config.")

        with tempfile.TemporaryDirectory() as tmpdir:
            logger.debug(
                f"Content of the dbt project dir {self.render_config.project_path}: `{os.listdir(self.render_config.project_path)}`"
            )
            tmpdir_path = Path(tmpdir)

            create_symlinks(project_path, tmpdir_path, self.render_config.dbt_deps)

            if self.project.partial_parse and self.cache_dir:
                latest_partial_parse = cache._get_latest_partial_parse(project_path, self.cache_dir)
                logger.info("Partial parse is enabled and the latest partial parse file is %s", latest_partial_parse)
                if latest_partial_parse is not None:
                    cache._copy_partial_parse_to_project(latest_partial_parse, tmpdir_path)

            with self.profile_config.ensure_profile(
                use_mock_values=self.render_config.enable_mock_profile
            ) as profile_values, environ(self.env_vars):
                (profile_path, env_vars) = profile_values
                env = os.environ.copy()
                env.update(env_vars)

                self.local_flags = [
                    "--project-dir",
                    str(tmpdir),
                    "--profiles-dir",
                    str(profile_path.parent),
                    "--profile",
                    self.profile_config.profile_name,
                    "--target",
                    self.profile_config.target_name,
                ]
                self.log_dir = Path(env.get(DBT_LOG_PATH_ENVVAR) or tmpdir_path / DBT_LOG_DIR_NAME)
                self.target_dir = Path(env.get(DBT_TARGET_PATH_ENVVAR) or tmpdir_path / DBT_TARGET_DIR_NAME)
                env[DBT_LOG_PATH_ENVVAR] = str(self.log_dir)
                env[DBT_TARGET_PATH_ENVVAR] = str(self.target_dir)

                if self.render_config.dbt_deps and has_non_empty_dependencies_file(self.project_path):
                    deps_command = [dbt_cmd, "deps"]
                    deps_command.extend(self.local_flags)
                    stdout = run_command(deps_command, tmpdir_path, env)
                    logger.debug("dbt deps output: %s", stdout)

                nodes = self.run_dbt_ls(dbt_cmd, self.project_path, tmpdir_path, env)

                self.nodes = nodes
                self.filtered_nodes = nodes

            if self.project.partial_parse and self.cache_dir:
                partial_parse_file = get_partial_parse_path(tmpdir_path)
                if partial_parse_file.exists():
                    cache._update_partial_parse_cache(partial_parse_file, self.cache_dir)

    def load_via_dbt_ls_file(self) -> None:
        """
        This is between dbt ls and full manifest. It allows to use the output (needs to be json output) of the dbt ls as a
        file stored in the image you run Cosmos on. The advantage is that you can use the parser from LoadMode.DBT_LS without
        actually running dbt ls every time. BUT you will need one dbt ls file for each separate group.

        This technically should increase performance and also removes the necessity to have your whole dbt project copied
        to the airflow image.
        """
        self.load_method = LoadMode.DBT_LS_FILE
        logger.info("Trying to parse the dbt project `%s` using a dbt ls output file...", self.project.project_name)

        if not self.render_config.is_dbt_ls_file_available():
            raise CosmosLoadDbtException(f"Unable to load dbt ls file using {self.render_config.dbt_ls_path}")

        project_path = self.render_config.project_path
        if not project_path:
            raise CosmosLoadDbtException("Unable to load dbt ls file without RenderConfig.project_path")
        with open(self.render_config.dbt_ls_path) as fp:  # type: ignore[arg-type]
            dbt_ls_output = fp.read()
            nodes = parse_dbt_ls_output(project_path=project_path, ls_stdout=dbt_ls_output)

        self.nodes = nodes
        self.filtered_nodes = nodes

    def load_via_custom_parser(self) -> None:
        """
        This is the least accurate way of loading `dbt` projects and filtering them out, since it uses custom Cosmos
        logic, which is usually a subset of what is available in `dbt`.

        Internally, it uses the legacy Cosmos DbtProject representation and converts it to the current
        nodes list representation.

        Updates in-place:
        * self.nodes
        * self.filtered_nodes
        """
        self.load_method = LoadMode.CUSTOM
        logger.info("Trying to parse the dbt project `%s` using a custom Cosmos method...", self.project.project_name)

        if self.render_config.selector:
            raise CosmosLoadDbtException(
                "RenderConfig.selector is not yet supported when loading dbt projects using the LoadMode.CUSTOM parser."
            )

        if not self.render_config.project_path or not self.execution_config.project_path:
            raise CosmosLoadDbtException(
                "Unable to load dbt project without RenderConfig.dbt_project_path and ExecutionConfig.dbt_project_path"
            )

        project = LegacyDbtProject(
            project_name=self.render_config.project_path.stem,
            dbt_root_path=self.render_config.project_path.parent.as_posix(),
            dbt_models_dir=self.project.models_path.stem if self.project.models_path else "models",
            dbt_seeds_dir=self.project.seeds_path.stem if self.project.seeds_path else "seeds",
            dbt_vars=self.dbt_vars,
        )
        nodes = {}
        models = itertools.chain(
            project.models.items(), project.snapshots.items(), project.seeds.items(), project.tests.items()
        )
        for model_name, model in models:
            config = {item.split(":")[0]: item.split(":")[-1] for item in model.config.config_selectors}
            node = DbtNode(
                unique_id=f"{model.type.value}.{self.project.project_name}.{model_name}",
                resource_type=DbtResourceType(model.type.value),
                depends_on=list(model.config.upstream_models),
                file_path=Path(
                    model.path.as_posix().replace(
                        self.render_config.project_path.as_posix(), self.execution_config.project_path.as_posix()
                    )
                ),
                tags=[],
                config=config,
            )
            nodes[model_name] = node

        self.nodes = nodes
        self.filtered_nodes = select_nodes(
            project_dir=self.execution_config.project_path,
            nodes=nodes,
            select=self.render_config.select,
            exclude=self.render_config.exclude,
        )

    def load_from_dbt_manifest(self) -> None:
        """
        This approach accurately loads `dbt` projects using the `manifest.yml` file.

        However, since the Manifest does not represent filters, it relies on the Custom Cosmos implementation
        to filter out the nodes relevant to the user (based on self.exclude and self.select).

        Updates in-place:
        * self.nodes
        * self.filtered_nodes
        """
        self.load_method = LoadMode.DBT_MANIFEST
        logger.info("Trying to parse the dbt project `%s` using a dbt manifest...", self.project.project_name)

        if self.render_config.selector:
            raise CosmosLoadDbtException(
                "RenderConfig.selector is not yet supported when loading dbt projects using the LoadMode.DBT_MANIFEST parser."
            )

        if not self.project.is_manifest_available():
            raise CosmosLoadDbtException(f"Unable to load manifest using {self.project.manifest_path}")

        if not self.execution_config.project_path:
            raise CosmosLoadDbtException("Unable to load manifest without ExecutionConfig.dbt_project_path")

        nodes = {}
        with open(self.project.manifest_path) as fp:  # type: ignore[arg-type]
            manifest = json.load(fp)

            resources = {**manifest.get("nodes", {}), **manifest.get("sources", {}), **manifest.get("exposures", {})}
            for unique_id, node_dict in resources.items():
                node = DbtNode(
                    unique_id=unique_id,
                    resource_type=DbtResourceType(node_dict["resource_type"]),
                    depends_on=node_dict.get("depends_on", {}).get("nodes", []),
                    file_path=self.execution_config.project_path / Path(node_dict["original_file_path"]),
                    tags=node_dict["tags"],
                    config=node_dict["config"],
                )

                nodes[node.unique_id] = node

            self.nodes = nodes
            self.filtered_nodes = select_nodes(
                project_dir=self.execution_config.project_path,
                nodes=nodes,
                select=self.render_config.select,
                exclude=self.render_config.exclude,
            )

    def update_node_dependency(self) -> None:
        """
        This will update the property `has_test` if node has `dbt` test

        Updates in-place:
        * self.filtered_nodes
        """
        for _, node in list(self.nodes.items()):
            if node.resource_type == DbtResourceType.TEST:
                for node_id in node.depends_on:
                    if node_id in self.filtered_nodes:
                        self.filtered_nodes[node_id].has_test = True
                        self.filtered_nodes[node.unique_id] = node
