"""Fakes for the AWS clients, so every test runs offline."""

from mtw import datapi


class FakeDataClient:
    """Stands in for boto3's redshift-data client. `script` maps a substring of the SQL to a response."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []
        self._results = {}
        self._status = {}
        self._counter = 0

    def _respond(self, sql):
        for needle, response in self.script.items():
            if needle in sql:
                return response
        return {"rows": []}

    def _new_id(self):
        self._counter += 1
        return f"id-{self._counter}"

    def execute_statement(self, Sql, **kwargs):
        sid = self._new_id()
        self.calls.append(("execute", Sql, kwargs))
        response = self._respond(Sql)
        self._status[sid] = response
        return {"Id": sid}

    def batch_execute_statement(self, Sqls, **kwargs):
        sid = self._new_id()
        self.calls.append(("batch", list(Sqls), kwargs))
        responses = [self._respond(sql) for sql in Sqls]
        error = next((r for r in responses if r.get("error")), None)
        self._status[sid] = {"batch": responses, "error": error["error"] if error else None}
        return {"Id": sid}

    def describe_statement(self, Id):
        entry = self._status[Id]
        if entry.get("error"):
            return {"Status": "FAILED", "Error": entry["error"]}
        if "batch" in entry:
            subs = []
            for index, response in enumerate(entry["batch"]):
                sub_id = f"{Id}:{index + 1}"
                self._results[sub_id] = response.get("rows", [])
                subs.append({"Id": sub_id, "Duration": int(response.get("ms", 1) * 1_000_000), "HasResultSet": bool(response.get("rows"))})
            return {"Status": "FINISHED", "SubStatements": subs}
        self._results[Id] = entry.get("rows", [])
        return {"Status": "FINISHED", "HasResultSet": bool(entry.get("rows"))}

    def get_statement_result(self, Id, NextToken=None):
        rows = self._results[Id]
        columns = list(rows[0].keys())
        records = []
        for row in rows:
            record = []
            for column in columns:
                value = row[column]
                if value is None:
                    record.append({"isNull": True})
                elif isinstance(value, bool):
                    record.append({"booleanValue": value})
                elif isinstance(value, int):
                    record.append({"longValue": value})
                elif isinstance(value, float):
                    record.append({"doubleValue": value})
                else:
                    record.append({"stringValue": str(value)})
            records.append(record)
        return {"ColumnMetadata": [{"name": c} for c in columns], "Records": records}


def fake_api(script=None):
    target = datapi.Target("serverless", "wg", "arn:secret")
    client = FakeDataClient(script)
    return datapi.DataApi(target, client=client, sleep=lambda s: None), client


class FakeApi:
    """A higher-level fake for modules that only call execute / execute_quiet / query_one / timed_batch."""

    def __init__(self, counts=None, batch_rows=None, fail_on=None, ms=10.0):
        self.statements = []
        self.counts = counts or {}
        self.batch_rows = batch_rows or {}
        self.fail_on = fail_on or {}
        self.ms = ms

    def _maybe_fail(self, sql):
        for needle, message in self.fail_on.items():
            if needle in sql:
                raise datapi.SqlError(sql, message)

    def execute(self, sql):
        self.statements.append(sql)
        self._maybe_fail(sql)
        return []

    def execute_quiet(self, sql, ignore):
        self.statements.append(sql)
        try:
            self._maybe_fail(sql)
        except datapi.SqlError as error:
            if any(p.lower() in error.message.lower() for p in ignore):
                return False
            raise
        return True

    def query_one(self, sql):
        self.statements.append(sql)
        for needle, value in self.counts.items():
            if needle in sql:
                return value
        return {"n": 0}

    def timed_batch(self, statements):
        self.statements.extend(statements)
        for sql in statements:
            self._maybe_fail(sql)
        out = []
        for sql in statements:
            rows = []
            for needle, value in self.batch_rows.items():
                if needle in sql:
                    rows = value
            out.append({"sql": sql, "duration_ms": self.ms, "rows": rows})
        return out


class FakeS3:
    def __init__(self, keys=None):
        self.keys = dict(keys or {})  # key -> size

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        items = [{"Key": k, "Size": v} for k, v in sorted(self.keys.items()) if k.startswith(Prefix)]
        return {"Contents": items, "IsTruncated": False}


def expect_error(func, *args, exception=Exception, contains="", **kwargs):
    try:
        func(*args, **kwargs)
    except exception as error:
        assert contains in str(error), f"{contains!r} not in {error!r}"
        return error
    raise AssertionError("expected an error")
