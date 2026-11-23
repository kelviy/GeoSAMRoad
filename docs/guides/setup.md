# Setup

Set up the repo, its Python environment, QGIS and the review queue plugin on Linux or macOS.

You do this once per computer. When you finish, QGIS 3.44 or later is installed, the **ROSA review queue** plugin is enabled, and your computer account has a full name for QGIS to record on your edits. Terms such as zone package and review queue are defined in the [glossary](../glossary.md).

!!! note "Linux and macOS"
    Every step below has been run on Linux and on macOS. Where the two differ, the step gives both. Windows is not covered.

## Clone the repo

The cleaning tools live in the GeoSAMRoad repo. Until the cleaning work merges into `main`, it is on the `semi-automation` branch.

```sh
git clone https://github.com/kelviy/GeoSAMRoad.git
cd GeoSAMRoad
git checkout semi-automation
```

The rest of the guides call this folder `<repo>`.

## Build the Python environment

The repo uses [uv](https://docs.astral.sh/uv/) to manage its Python environment. It needs Python 3.12 or later, and uv downloads one if your computer has none.

1. Install uv by following the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
2. In `<repo>`, build the environment:

    ```sh
    uv sync
    ```

    This creates `<repo>/.venv` with the dataset and cleaning tools, the tests, Ruff and Pyright. That is all you need to clean labels.

3. If you also train or run models, install every extra instead. That adds PyTorch and the model libraries, which are several gigabytes:

    ```sh
    uv sync --all-extras
    ```

4. Check that the CLI runs. It prints a list of commands:

    ```sh
    .venv/bin/python -m sentinel2data.cli --help
    ```

The lock file covers Linux on x86-64 and macOS on Apple silicon only. An Intel Mac is not supported.

!!! danger "Never use `uv run` for the dev tools"
    Run Python, Ruff and Pyright straight from the venv, for example `.venv/bin/ruff check <paths>` and `.venv/bin/pyright <paths>`. `uv run` first syncs `.venv` to the default dependency groups. That sync uninstalls every extra you added with `--all-extras`, such as `torch` and `terratorch`. A plain `uv sync` does the same, so add `--all-extras` each time you sync if you use the extras.

## Install QGIS

Install QGIS 3.44 or later. The repo's QGIS tests run on QGIS 3.44.

- **Linux.** Use the official installer for your distribution from the [QGIS download page](https://qgis.org/download/). The route used on Linux so far is conda, with QGIS in its own environment:

    ```sh
    conda create -n qgis_env -c conda-forge "qgis>=3.44"
    conda activate qgis_env
    qgis
    ```

- **macOS**. Download the official macOS installer from the [QGIS download page](https://qgis.org/download/) and run it.

Start QGIS once to check that it opens. Then close it.

!!! note "Screenshot: the empty QGIS window after the first start"

## Install the review queue plugin

The plugin docks the review queue in QGIS. QGIS's own Python lacks the libraries the review queue needs, so the plugin runs them in the repo's Python. The install command records which Python that is.

1. Close QGIS.
2. In `<repo>`, run the install command with the venv's Python:

    ```sh
    .venv/bin/python -m sentinel2data.cli install-qgis-plugin
    ```

    It prints the plugin folder it installed into and the Python it recorded. By default it installs into your `default` QGIS profile:

    - Linux: `~/.local/share/QGIS/QGIS3/profiles/default`
    - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default`

3. If QGIS uses another profile folder, give that folder with `--profile-dir`. To find it, in QGIS select **Settings > User Profiles > Open Active Profile Folder**.

    ```sh
    .venv/bin/python -m sentinel2data.cli install-qgis-plugin --profile-dir <profile-dir>
    ```

4. Start QGIS.
5. Select **Plugins > Manage and Install Plugins**, then select **Installed**.
6. Select the check box next to **ROSA review queue**.

!!! note "Screenshot: the Installed tab with ROSA review queue checked"

Run the install command again after each `git pull`, and after you move `<repo>` or rebuild `.venv` somewhere else. The command replaces the old copy of the plugin.

## Set your full name

QGIS records the full name of your computer account as the annotator on every edit and every decision. If your account has no full name, the review queue panel refuses to write and says so. Set it before you clean a zone. Use your real name in the form `Jane Doe`.

**Linux.** Check the current full name. An empty line, or one that starts with a comma, means none is set:

```sh
getent passwd "$USER" | cut -d: -f5
```

To set it, run the command below, or use the **Users** page of your desktop settings:

```sh
sudo chfn -f "Jane Doe" "$USER"
```

**macOS**. Check the current full name:

```sh
id -F
```

To change it, open **System Settings > Users & Groups**. Control-click your account and select **Advanced Options**. Change **Full name**, then log out and in again.

On both systems, restart QGIS after you change the name. QGIS reads it only when it starts.

Next, find the files you need on [Data & files](data-and-files.md), then [claim and download a zone](claim-and-download.md).
