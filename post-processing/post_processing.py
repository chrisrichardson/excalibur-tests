import argparse
import ast
import errno
import math
import operator as op
import os
import re
import traceback
from functools import reduce
from itertools import chain
from pathlib import Path

import pandas as pd
import yaml
from bokeh.models import Legend, HoverTool
from bokeh.models.sources import ColumnDataSource
from bokeh.palettes import viridis
from bokeh.plotting import figure, output_file, save
from bokeh.transform import factor_cmap

class PostProcessing:

    def __init__(self, debug=False, verbose=False):
        self.debug = debug
        self.verbose = verbose

    def run_post_processing(self, log_path, config):
        """
            Return a dataframe containing the information passed to a plotting script and produce relevant graphs.

            Args:
                log_path: str, path to a log file or a directory containing log files.
                config: dict, configuration information for plotting.
        """

        log_files = []
        # look for perflogs
        if os.path.isfile(log_path):
            if os.path.splitext(log_path)[1] != ".log":
                raise RuntimeError("Perflog file name provided should have a .log extension.")
            log_files = [log_path]
        elif os.path.isdir(log_path):
            log_files_temp = [os.path.join(root, file) for root, _, files in os.walk(log_path) for file in files]
            for file in log_files_temp:
                if os.path.splitext(file)[1] == ".log":
                    log_files.append(file)
            if len(log_files) == 0:
                raise RuntimeError("No perflogs found in this path. Perflogs should have a .log extension.")
        else:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), log_path)

        if self.debug:
            print("Found log files:")
            for log in log_files:
                print("-", log)
            print("")

        df = pd.DataFrame()
        # put all perflog information in one dataframe
        for file in log_files:
            try:
                temp = read_perflog(file)
                df = pd.concat([df, temp], ignore_index=True)
            except KeyError as e:
                if self.debug:
                    print("Discarding %s:" %os.path.basename(file), type(e).__name__ + ":", e.args[0], e.args[1])
                    print("")
        if df.empty:
            raise FileNotFoundError(errno.ENOENT, "Could not find a valid perflog in path", log_path)

        # get axis columns
        columns = [config["x_axis"]["value"], config["y_axis"]["value"]]
        if config["x_axis"]["units"].get("column"):
            columns.insert(1, config["x_axis"]["units"]["column"])
        if config["y_axis"]["units"].get("column"):
            columns.append(config["y_axis"]["units"]["column"])

        series = config["series"]
        # extract series columns and filters
        series_columns = [s[0] for s in series]
        series_filters = [[s[0], "==", s[1]] for s in series]
        # check acceptable number of series
        if len(set(series_columns)) > 1:
            raise RuntimeError("Currently supporting grouping of series by only one column. Please use a single column name in your series configuration.")
        # add series columns to column list
        for c in series_columns:
            if c not in columns:
                columns.append(c)

        filters = config["filters"]
        # extract filter columns
        filter_columns = [f[0] for f in filters]
        # gather all relevant columns
        all_columns = columns + filter_columns

        invalid_columns = []
        # check for invalid columns
        for col in all_columns:
            if col not in df.columns:
                invalid_columns.append(col)
        if invalid_columns:
            raise KeyError("Could not find columns", invalid_columns)

        # apply user-specified types to all relevant columns
        for col in all_columns:
            if config["column_types"].get(col):

                # get user input type
                conversion_type = config["column_types"][col]
                # allow user to specify "datetime" as a type (internally convert to "datetime64")
                conversion_type += "64" if conversion_type == "datetime" else ""

                # internal type conversion
                if pd.api.types.is_string_dtype(conversion_type):
                    # all strings treated as object (nullable)
                    conversion_type = "object"
                elif pd.api.types.is_float_dtype(conversion_type):
                    # all floats treated as float64 (nullable)
                    conversion_type = "float64"
                elif pd.api.types.is_integer_dtype(conversion_type):
                    # all integers treated as Int64 (nullable)
                    # note: default pandas integer type is int64 (not nullable)
                    conversion_type = "Int64"
                elif pd.api.types.is_datetime64_any_dtype(conversion_type):
                    # all datetimes treated as datetime64[ns] (nullable)
                    conversion_type = "datetime64[ns]"
                else:
                    raise RuntimeError("Unsupported user-specified type '{0}' for column '{1}'.".format(conversion_type, col))

                # skip type conversion if column is already the desired type
                if conversion_type == df[col].dtype:
                    continue
                # otherwise apply type to column
                df[col] = df[col].astype(conversion_type)

            else:
                raise KeyError("Could not find user-specified type for column", col)

        mask = pd.Series(df.index.notnull())
        # filter rows
        if filters:
            mask = reduce(op.and_, (self.row_filter(f, df) for f in filters))
        # apply series filters
        if series_filters:
            series_mask = reduce(op.or_, (self.row_filter(f, df) for f in series_filters))
            mask = mask & series_mask
        # ensure not all rows are filtered away
        if df[mask].empty:
            raise pd.errors.EmptyDataError("Filtered dataframe is empty", df[mask].index)

        # get number of occurrences of each column
        series_col_count = {c:series_columns.count(c) for c in series_columns}
        # get number of column combinations
        series_combinations = reduce(op.mul, list(series_col_count.values()), 1)

        num_filtered_rows = len(df[mask])
        num_x_data_points = series_combinations * len(set(df[config["x_axis"]["value"]][mask]))
        # check expected number of rows
        if num_filtered_rows > num_x_data_points:
            raise RuntimeError("Unexpected number of rows ({0}) does not match number of unique x-axis values per series ({1})".format(num_filtered_rows, num_x_data_points), df[columns][mask])

        print("Selected dataframe:")
        print(df[columns][mask])

        # call a plotting script
        self.plot_generic(config["title"], df[columns][mask], config["x_axis"], config["y_axis"], series_filters)

        if self.debug & self.verbose:
            print("")
            print("Full dataframe:")
            print(df.to_json(orient="columns", indent=2))

        return df[columns][mask]

    def plot_generic(self, title, df: pd.DataFrame, x_axis, y_axis, series_filters):
        """
            Create a bar chart for the supplied data using bokeh.

            Args:
                title: str, plot title (read from config).
                df: dataframe, data to plot.
                x_axis: dict, x-axis column and units (read from config).
                y_axis: dict, y-axis column and units (read from config).
                series_filters: list, x-axis groups used to filter graph data.
        """

        # get column names and labels for axes
        x_column, x_label = get_axis_info(df, x_axis)
        y_column, y_label = get_axis_info(df, y_axis)

        # find x-axis groups (series columns)
        groups = [x_column]
        for f in series_filters:
            if f[0] not in groups:
                groups.append(f[0])
        # all x-axis data treated as categorical
        for g in groups:
            df[g] = df[g].astype(str)
        # combine group names for later plotting with groupby
        index_group_col = "_".join(groups)
        # group by group names (or just x-axis if no other groups are present)
        grouped_df = df.groupby(x_column) if len(groups) == 1 else df.groupby(groups)

        if self.debug:
            print("")
            print("Plot x-axis groups:")
            for key, _ in grouped_df:
                print(grouped_df.get_group(key))

        # adjust y-axis range
        min_y = 0 if min(df[y_column]) >= 0 \
                else math.floor(min(df[y_column])*1.2)
        max_y = 0 if max(df[y_column]) <= 0 \
                else math.ceil(max(df[y_column])*1.2)

        # create html file to store plot in
        output_file(filename=os.path.join(Path(__file__).parent, "{0}.html".format(title.replace(" ", "_"))), title=title)

        # create plot
        plot = figure(x_range=grouped_df, y_range=(min_y, max_y), title=title, width=800, toolbar_location="above")
        # configure tooltip
        plot.add_tools(HoverTool(tooltips=[(y_label, "@{0}_mean".format(y_column)
                                            + ("{%0.2f}" if pd.api.types.is_float_dtype(df[y_column].dtype) else ""))],
                                 formatters={"@{0}_mean".format(y_column) : "printf"}))

        # create legend outside plot
        plot.add_layout(Legend(), "right")
        # automatically base bar colouring on last group column
        colour_factors = sorted(df[groups[-1]].unique())
        # divide and assign colours
        index_cmap = factor_cmap(index_group_col, palette=viridis(len(colour_factors)), factors=colour_factors, start=len(groups)-1, end=len(groups))
        # add legend labels to data source
        data_source = ColumnDataSource(grouped_df).data
        legend_labels = ["{0} = {1}".format(groups[-1].replace("_", " "), group[-1]) for group in data_source[index_group_col]]
        data_source["legend_labels"] = legend_labels

        # add bars
        plot.vbar(x=index_group_col, top="{0}_mean".format(y_column), width=0.9, source=data_source, line_color="white", fill_color=index_cmap, legend_field="legend_labels", hover_alpha=0.9)
        # add labels
        plot.xaxis.axis_label = x_label
        plot.yaxis.axis_label = y_label
        # adjust font size
        plot.title.text_font_size = "15pt"

        # save to file
        save(plot)

    # operator lookup dictionary
    op_lookup = {
        "==":   op.eq,
        "!=":   op.ne,
        "<" :   op.lt,
        ">" :   op.gt,
        "<=":   op.le,
        ">=":   op.ge
    }

    def row_filter(self, filter, df: pd.DataFrame):
        """
            Return a dataframe mask based on a filter condition. The filter is a list that contains a column name, an operator, and a value (e.g. ["flops_value", ">=", 1.0]).

            Args:
                filter: list, a condition based on which a dataframe is filtered.
                df: dataframe, used to create a mask by having the filter condition applied to it.
        """

        column, str_op, value = filter
        if self.debug:
            print("Applying row filter condition:", column, str_op, value)

        # check operator validity
        operator = self.op_lookup.get(str_op)
        if operator is None:
            raise KeyError("Unknown comparison operator", str_op)

        # evaluate expression and extract dataframe mask
        if value is None:
            mask = df[column].isnull() if operator == op.eq else df[column].notnull()
        else:
            try:
                # interpret comparison value as column dtype
                value = pd.Series(value, dtype=df[column].dtype).iloc[0]
                mask = operator(df[column], value)
            except TypeError or ValueError as e:
                e.args = (e.args[0] + " for column: \'{0}\' and value: \'{1}\'".format(column, value),)
                raise

        if self.debug & self.verbose:
            print(mask)
        if self.debug:
            print("")

        return mask

def read_args():
    """
        Return parsed command line arguments.
    """

    parser = argparse.ArgumentParser(description="Plot benchmark data. At least one perflog must be supplied.")

    # required positional arguments (log path, config path)
    parser.add_argument("log_path", type=str, help="path to a perflog file or a directory containing perflog files")
    parser.add_argument("config_path", type=str, help="path to a configuration file specifying what to plot")

    # optional argument (plot type)
    parser.add_argument("-p", "--plot_type", type=str, default="generic", help="type of plot to be generated (default: \'generic\')")

    # info dump flags
    parser.add_argument("-d", "--debug", action="store_true", help="debug flag for printing additional information")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose flag for printing more debug information (must be used in conjunction with the debug flag)")

    return parser.parse_args()

def read_config(path):
    """
        Return a dictionary containing configuration information for plotting.

        Args:
            path: str, path to a config file.
    """

    with open(path, "r") as file:
        config = yaml.safe_load(file)

    # check x-axis information
    if not config.get("x_axis"):
        raise KeyError("Missing x-axis information")
    if not config.get("x_axis").get("value"):
        raise KeyError("Missing x-axis value information")
    if not config.get("x_axis").get("units"):
        raise KeyError("Missing x-axis units information")
    # check y-axis information
    if not config.get("y_axis"):
        raise KeyError("Missing y-axis information")
    if not config.get("y_axis").get("value"):
        raise KeyError("Missing y-axis value information")
    if not config.get("y_axis").get("units"):
        raise KeyError("Missing y-axis units information")

    # check series length
    if config.get("series") is None:
        raise KeyError("Missing series information (specify an empty list [] if there is only one series)")
    if len(config["series"]) == 1:
        raise KeyError("Number of series must be >= 2 (specify an empty list [] if there is only one series)")

    # check filters are present
    if config.get("filters") is None:
        raise KeyError("Missing filters information (specify an empty list [] if none are required)")

    # check plot title information
    if not config.get("title"):
        raise KeyError("Missing plot title information")

    return config

# a modified and updated version of the function from perf_logs.py
def read_perflog(path):
    """
        Return a pandas dataframe from a ReFrame performance log.

        Args:
            path: str, path to log file.

        NB: This currently depends on having a non-default handlers_perflog.filelog.format in reframe's configuration. See code.

        The returned dataframe will have columns for all fields in a performance log record
        except display name, extra resources, and env vars. Display name will be broken up
        into test name and parameter columns, while the other two will be replaced by the
        dictionary contents of their fields (keys become columns, values become row contents).
    """

    # read perflog into dataframe
    df = pd.read_csv(path, delimiter="|")
    REQUIRED_LOG_FIELDS = ["job_completion_time", r"\w+_value$", r"\w+_unit$", "display_name"]

    # look for required column matches
    required_field_matches = [len(list(filter(re.compile(rexpr).match, df.columns))) > 0 for rexpr in REQUIRED_LOG_FIELDS]
    # check all required columns are present
    if False in required_field_matches:
        raise KeyError("Perflog missing one or more required fields", REQUIRED_LOG_FIELDS)

    # replace display name
    results = df["display_name"].apply(get_display_name_info)
    index = df.columns.get_loc("display_name")
    # insert new columns and contents
    insert_key_cols(df, index, [r[1] for r in results])
    df.insert(index, "test_name", [r[0] for r in results])
    # drop old column
    df.drop("display_name", axis=1, inplace=True)

    # replace other columns with dictionary contents
    dict_cols = [c for c in ["extra_resources", "env_vars"] if c in df.columns]
    for col in dict_cols:
        results = df[col].apply(lambda x: ast.literal_eval(x))
        # insert new columns and contents
        insert_key_cols(df, df.columns.get_loc(col), results)
        # drop old column
        df.drop(col, axis=1, inplace=True)

    return df

def get_display_name_info(display_name):
    """
        Return a tuple containing the test name and a dictionary of parameter names and their values from the given input string. The parameter dictionary may be empty if no parameters are present.

        Args:
            display_name: str, expecting a format of <test_name> followed by zero or more %<param>=<value> pairs.
    """

    split_display_name = display_name.split(" %")
    test_name = split_display_name[0]
    params = [p.split("=") for p in split_display_name[1:]]

    return test_name, dict(params)

def insert_key_cols(df: pd.DataFrame, index, results):
    """
        Modify a dataframe to include new columns (extracted from results) inserted at a given index.

        Args:
            df: dataframe, to be modified by this function.
            index: int, index as which to insert new columns into the dataframe.
            results: dict list, contains key-value mapping information for all rows.
    """
    # get set of keys from all rows
    keys = set(chain.from_iterable([r.keys() for r in results]))
    for k in keys:
        # insert keys as new columns
        df.insert(index, k, [r[k] if k in r.keys() else None for r in results])

def get_axis_info(df: pd.DataFrame, axis):
    """
        Return the column name and label for a given axis. If a column name is supplied as units information, the actual units will be extracted from a dataframe.

        Args:
            df: dataframe, data to plot.
            axis: dict, axis column and units.
    """

    # get column name of axis
    col_name = axis.get("value")
    # get units
    units = axis.get("units").get("custom")
    if axis.get("units").get("column"):
        unit_set = set(df[axis["units"]["column"]].dropna())
        # check all rows have the same units
        if len(unit_set) != 1:
            raise RuntimeError("Unexpected number of axis unit entries {0}".format(unit_set))
        units = next(iter(unit_set))
    # determine axis label
    label = "{0}{1}".format(col_name.replace("_", " ").title(),
                            " ({0})".format(units) if units else "")

    return col_name, label

def main():

    args = read_args()
    post = PostProcessing(args.debug, args.verbose)

    try:
        config = read_config(args.config_path)
        post.run_post_processing(args.log_path, config)

    except Exception as e:
        print(type(e).__name__ + ":", e)
        print("Post-processing stopped")
        if args.debug:
            print(traceback.format_exc())

if __name__ == "__main__":
    main()
