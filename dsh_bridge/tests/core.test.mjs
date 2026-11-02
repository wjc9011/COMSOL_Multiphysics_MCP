import { test, after } from "node:test";
import assert from "node:assert/strict";
import { spawnFakeServer, makeConnection } from "./helpers.mjs";

const server = spawnFakeServer();
after(() => server.close());

let syncCount = 0;
let lastTools = [];
const conn = makeConnection(server, {
	onTools: (tools) => { syncCount += 1; lastTools = tools; },
});

after(async () => { conn.dispose(); });

test("connect discovers the full tool surface", async () => {
	const ok = await conn.connect();
	assert.equal(ok, true);
	assert.equal(conn.connected, true);
	const names = lastTools.map((t) => t.name);
	for (const expected of ["capabilities", "job_submit", "job_status", "job_tail", "job_cancel", "stats", "die"]) {
		assert.ok(names.includes(expected), `missing tool ${expected}`);
	}
});

test("tools/call returns canonical text projection", async () => {
	const res = await conn.callTool("capabilities", {});
	assert.ok(res.text.includes("package_version"), res.text);
	assert.ok(Array.isArray(res.content));
});

test("serialization: never more than one in-flight request", async () => {
	await Promise.all(Array.from({ length: 10 }, () => conn.callTool("job_status", { job_id: "j" })));
	const stats = await conn.callTool("stats", {});
	const parsed = JSON.parse(stats.text);
	assert.equal(parsed.maxInFlight, 1, `expected maxInFlight=1, got ${parsed.maxInFlight}`);
});

test("aborted signal rejects the call", async () => {
	const before = JSON.parse((await conn.callTool("stats", {})).text).submitCount;
	const ac = new AbortController();
	ac.abort();
	await assert.rejects(conn.callTool("job_submit", { spec: { job_type: "staged_sweep" } }, { signal: ac.signal }), /aborted/);
	const after = JSON.parse((await conn.callTool("stats", {})).text).submitCount;
	assert.equal(after, before, "a pre-aborted request must not create a server job");
	const res = await conn.callTool("capabilities", {});
	assert.ok(res.text.includes("package_version"));
});

test("per-call timeout rejects, the late response keeps single-flight occupancy, and the connection stays usable", { timeout: 15000 }, async () => {
	// C081Q-01: timing the caller's 5 ms timeout against the shared
	// server's 10 ms handling delay is a cross-process timer race - on a
	// loaded hosted runner the caller's one-shot timer can fire after the
	// server's response, resolving a call the test expects to reject. A
	// dedicated slow server (1000 ms delay vs a 5 ms timeout) makes the
	// outcome deterministic while keeping the same assertions: no product
	// timeout changed, no assertion removed.
	const slow = spawnFakeServer({ FAKE_DELAY_MS: "1000" });
	try {
		const slowConn = makeConnection(slow, {});
		await slowConn.connect();
		await assert.rejects(slowConn.callTool("capabilities", {}, { timeoutMs: 5 }), /timed out/);
		// The late (post-timeout) response settles the pending entry and
		// releases occupancy; the next call reuses the same connection.
		const res = await slowConn.callTool("capabilities", {});
		assert.ok(res.text.includes("package_version"));
		const stats = JSON.parse((await slowConn.callTool("stats", {})).text);
		assert.equal(stats.maxInFlight, 1, `expected maxInFlight=1, got ${stats.maxInFlight}`);
		slowConn.dispose();
	} finally {
		await slow.close();
	}
});

test("dispose rejects pending and subsequent calls", async () => {
	const c2 = makeConnection(server, {});
	await c2.connect();
	c2.dispose();
	await assert.rejects(c2.callTool("capabilities", {}), /disposed|not connected/);
});

test("reconnect: server exit respawns and re-discovers tools", { timeout: 15000 }, async () => {
	const before = syncCount;
	await conn.callTool("die", {});
	const deadline = Date.now() + 12000;
	while (syncCount <= before && Date.now() < deadline) {
		await new Promise((r) => setTimeout(r, 100));
	}
	assert.ok(syncCount > before, `expected a re-sync, syncCount=${syncCount} before=${before}`);
	assert.equal(conn.connected, true);
	const res = await conn.callTool("capabilities", {});
	assert.ok(res.text.includes("package_version"));
});

test("pagination: cursor walk assembles the full tool list", { timeout: 10000 }, async () => {
	const paged = spawnFakeServer({ FAKE_PAGE_SIZE: "2" });
	try {
		let tools = [];
		const pc = makeConnection(paged, { onTools: (t) => { tools = t; } });
		await pc.connect();
		assert.equal(tools.length, 7, `expected 7 tools across pages, got ${tools.length}`);
		pc.dispose();
	} finally {
		await paged.close();
	}
});
