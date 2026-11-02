import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FIXTURE, spawnFakeServer, makeConnection, createFakeJobs, quietLogger } from "./helpers.mjs";
import { createComsolConnection } from "../lib/mcp-client-core.mjs";
import { createJobMirror } from "../lib/job-mirror.mjs";

// ---- MCP 未安装（not installed）----

test("missing executable fails closed", async () => {
	const dir = mkdtempSync(join(tmpdir(), "bridge-missing-"));
	try {
		const conn = createComsolConnection({ command: join(dir, "no-such-comsol-mcp.exe"), args: [], logger: quietLogger(), reconnect: { enabled: false } });
		const ok = await conn.connect();
		assert.equal(ok, false);
		assert.equal(conn.connected, false);
		conn.dispose();
	} finally { rmSync(dir, { recursive: true, force: true }); }
});

test("missing script argument fails closed", async () => {
	const dir = mkdtempSync(join(tmpdir(), "bridge-missingscript-"));
	try {
		const conn = createComsolConnection({ command: process.execPath, args: [join(dir, "gone.mjs")], logger: quietLogger(), reconnect: { enabled: false } });
		const ok = await conn.connect();
		assert.equal(ok, false);
		conn.dispose();
	} finally { rmSync(dir, { recursive: true, force: true }); }
});

// ---- MCP 损坏（corrupted）----

test("server crashing at startup fails closed", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "exit-startup" });
	try {
		const conn = makeConnection(s, { reconnect: { enabled: false } });
		const ok = await conn.connect();
		assert.equal(ok, false);
		conn.dispose();
	} finally { await s.close(); }
});

test("garbage on stdout is tolerated and the connection still works", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "garbage-first" });
	try {
		const conn = makeConnection(s, {});
		const ok = await conn.connect();
		assert.equal(ok, true);
		const res = await conn.callTool("capabilities", {});
		assert.ok(res.text.includes("package_version"));
		conn.dispose();
	} finally { await s.close(); }
});

test("responses split across stdout writes still parse (line framing)", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "split-write" });
	try {
		const conn = makeConnection(s, {});
		const ok = await conn.connect();
		assert.equal(ok, true);
		const res = await conn.callTool("capabilities", {});
		assert.ok(res.text.includes("package_version"));
		conn.dispose();
	} finally { await s.close(); }
});

// ---- 不支持的 MCP 版本 / 无效响应（unsupported / malformed）----

test("unsupported MCP protocol version fails closed", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "unsupported-version" });
	try {
		const conn = makeConnection(s, { reconnect: { enabled: false } });
		const ok = await conn.connect();
		assert.equal(ok, false);
		assert.equal(conn.connected, false);
		conn.dispose();
	} finally { await s.close(); }
});

test("server that never answers initialize fails after the init timeout", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "no-initialize" });
	try {
		const conn = makeConnection(s, { reconnect: { enabled: false }, initTimeoutMs: 300 });
		const ok = await conn.connect();
		assert.equal(ok, false);
		conn.dispose();
	} finally { await s.close(); }
});

test("initialize timeout releases single-flight occupancy for a healthy replacement", { timeout: 10000 }, async () => {
	const env = { FAKE_MODE: "no-initialize" };
	const conn = createComsolConnection({
		command: process.execPath,
		args: [FIXTURE],
		env,
		logger: quietLogger(),
		// C081R-02: the intended timeout triggers deterministically at ANY
		// budget because the no-initialize fixture never answers - so the
		// budget does not have to be small. A healthy replacement's
		// initialization budget also covers process spawn; 100 ms lost that
		// race on loaded runners. 2000 ms separates the two phases without
		// touching any product default (the test owns this value).
		initTimeoutMs: 2000,
		reconnect: { enabled: true, initialDelayMs: 20, maxDelayMs: 40, maxAttempts: 3 },
	});
	try {
		assert.equal(await conn.connect(), false);
		// Model a corrected child on the next bounded replacement attempt.
		env.FAKE_MODE = "";
		const deadline = Date.now() + 8000;
		while (!conn.connected && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
		assert.equal(conn.connected, true, "replacement should complete initialize and tool discovery");
		const result = await conn.callTool("capabilities", {});
		assert.match(result.text, /package_version/);
	} finally {
		conn.dispose();
	}
});

test("malformed tool list fails closed", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "malformed-tools" });
	try {
		const conn = makeConnection(s, { reconnect: { enabled: false } });
		const ok = await conn.connect();
		assert.equal(ok, false);
		conn.dispose();
	} finally { await s.close(); }
});

test("empty tool list is a valid discovery", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "no-tools" });
	try {
		let tools = null;
		const conn = makeConnection(s, { onTools: (t) => { tools = t; } });
		const ok = await conn.connect();
		assert.equal(ok, true);
		assert.deepEqual(tools, []);
		conn.dispose();
	} finally { await s.close(); }
});

test("isError responses reject the tool call", async () => {
	const s = spawnFakeServer({ FAKE_MODE: "call-error" });
	try {
		const conn = makeConnection(s, {});
		await conn.connect();
		await assert.rejects(conn.callTool("capabilities", {}), /boom/);
		conn.dispose();
	} finally { await s.close(); }
});

// ---- 恢复与预算（recovery / budgets）----

test("crash-looping server exhausts the reconnect budget and unregisters tools", { timeout: 20000 }, async () => {
	const s = spawnFakeServer({ FAKE_MODE: "exit-startup" });
	try {
		const calls = [];
		const conn = makeConnection(s, { reconnect: { enabled: true, initialDelayMs: 50, maxDelayMs: 100, maxAttempts: 3 }, onTools: (t) => calls.push(t.length) });
		await conn.connect();
		const deadline = Date.now() + 15000;
		while (Date.now() < deadline && !conn.budgetExhausted) {
			await new Promise((r) => setTimeout(r, 100));
		}
		assert.equal(conn.budgetExhausted, true);
		assert.ok(calls.includes(0), `expected an onTools([]) unregister, calls=${JSON.stringify(calls)}`);
		conn.dispose();
	} finally { await s.close(); }
});

test("server tools/list_changed notification triggers a re-sync", { timeout: 10000 }, async () => {
	const s = spawnFakeServer({ FAKE_MODE: "notify-list-changed" });
	try {
		let syncs = 0;
		const conn = makeConnection(s, { onTools: () => { syncs += 1; } });
		await conn.connect();
		const deadline = Date.now() + 8000;
		while (Date.now() < deadline && syncs < 2) {
			await new Promise((r) => setTimeout(r, 100));
		}
		assert.ok(syncs >= 2, `expected a re-sync, syncs=${syncs}`);
		conn.dispose();
	} finally { await s.close(); }
});

test("tool syncs coalesce instead of overlapping mid-pagination", { timeout: 20000 }, async () => {
	// One tool per page stretches the initial sync across the fixture's
	// 20ms list_changed notification; a slow async onTools would let two
	// uncoalesced syncs apply snapshots concurrently.
	const s = spawnFakeServer({ FAKE_MODE: "notify-list-changed", FAKE_PAGE_SIZE: "1" });
	try {
		let active = 0;
		let maxActive = 0;
		const applied = [];
		const sleepMs = (ms) => new Promise((r) => setTimeout(r, ms));
		const conn = makeConnection(s, {
			onTools: async (tools) => {
				active += 1;
				if (active > maxActive) maxActive = active;
				applied.push(tools.length);
				await sleepMs(300);
				active -= 1;
			},
		});
		await conn.connect();
		await sleepMs(1200); // initial sync plus the coalesced follow-up pass
		conn.dispose();
		assert.equal(maxActive, 1);
		assert.ok(applied.length >= 2, `expected a coalesced re-sync, applied=${JSON.stringify(applied)}`);
		assert.ok(applied.every((n) => n === 7), `every snapshot must be complete: ${JSON.stringify(applied)}`);
	} finally { await s.close(); }
});

// ---- 取消不可确认（cancel never confirmed）----

test("cancel without terminal confirmation settles killed at the deadline", { timeout: 10000 }, async () => {
	const s = spawnFakeServer({ FAKE_MODE: "ignore-cancel" });
	try {
		const jobs = createFakeJobs();
		const conn = makeConnection(s, {});
		await conn.connect();
		createJobMirror({ jobs, core: conn, jobId: "job-1", opts: { pollIntervalMs: 30, cancelConfirmTimeoutMs: 400 }, logger: quietLogger() });
		const rec = jobs.started[0];
		rec.hooks.cancel("caller cancelled");
		const outcome = await Promise.race([
			rec.done,
			new Promise((_, rej) => setTimeout(() => rej(new Error("cancel deadline never settled")), 8000)),
		]);
		assert.equal(outcome.status, "killed");
		assert.match(outcome.detail ?? "", /outcome unconfirmed/);
		conn.dispose();
	} finally { await s.close(); }
});
