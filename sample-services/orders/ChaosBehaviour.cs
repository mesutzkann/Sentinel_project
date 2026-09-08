using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Orders.Domain;
using Sentinel.Samples.Orders.Persistence;

namespace Sentinel.Samples.Orders;

/// <summary>
/// The runtime behaviour behind the orders scenarios that touch the database.
/// </summary>
/// <remarks>
/// Each of these genuinely changes what the service does, rather than logging that something
/// went wrong. The telemetry is a side effect, which is the only way an investigation over that
/// telemetry proves anything.
/// </remarks>
public sealed class ChaosBehaviour : IChaosActivationHandler
{
    private readonly ChaosRegistry _chaos;
    private readonly IServiceScopeFactory _scopeFactory;
    private readonly ILogger<ChaosBehaviour> _logger;

    /// <summary>
    /// Guards the one-time padding insert. Two concurrent requests arriving while the scenario
    /// is enabled would otherwise both start a 200k row insert.
    /// </summary>
    private readonly SemaphoreSlim _paddingLock = new(1, 1);

    private bool _padded;

    public ChaosBehaviour(
        ChaosRegistry chaos,
        IServiceScopeFactory scopeFactory,
        ILogger<ChaosBehaviour> logger)
    {
        _chaos = chaos;
        _scopeFactory = scopeFactory;
        _logger = logger;
    }

    /// <inheritdoc />
    /// <remarks>
    /// Scenario 2 needs a table big enough that scanning it is expensive. Growing it here rather
    /// than on the first request keeps the cost on the control call, where it belongs, and keeps
    /// the first measured request representative of the fault instead of the setup.
    /// </remarks>
    public async Task OnEnabledAsync(string code, CancellationToken cancellationToken)
    {
        if (code != ChaosCodes.DbSlowQueryMissingIndex)
        {
            return;
        }

        await using var scope = _scopeFactory.CreateAsyncScope();
        var db = scope.ServiceProvider.GetRequiredService<OrdersDbContext>();

        await EnsurePaddingAsync(db, cancellationToken);
    }

    /// <summary>
    /// Scenario 2 (DB_SLOW_QUERY_MISSING_INDEX): lists orders through a predicate no index can
    /// serve, over a table padded to a realistic size.
    /// </summary>
    /// <remarks>
    /// The healthy path filters on UserId or orders by CreatedAt, both indexed, so it stays fast
    /// on the same table — without that contrast the scenario would be indistinguishable from
    /// the service simply being slow. Here the filter is on Status and the sort on TotalAmount,
    /// neither of which is indexed, so PostgreSQL has to scan every row and then top-N sort the
    /// matches. The predicate deliberately matches most of the table: a filter that excluded
    /// almost everything would scan just as many rows but sort nothing, and the query would come
    /// back fast enough to look healthy.
    ///
    /// Deliberately produces no log line and no error: the evidence is a Prometheus latency
    /// series, an Npgsql span that dominates the trace, and pg_stat_statements.
    /// </remarks>
    public async Task<List<Order>> ListSlowlyAsync(
        OrdersDbContext db,
        int limit,
        CancellationToken cancellationToken)
    {
        // FromSql is defined on DbSet, so it has to come before AsNoTracking.
        return await db.Orders
            .FromSql(
                $"""
                 SELECT * FROM svc_orders.orders
                 WHERE "Status" = 'Paid'
                 ORDER BY "TotalAmount" DESC
                 LIMIT {limit}
                 """)
            .AsNoTracking()
            .ToListAsync(cancellationToken);
    }

    /// <summary>
    /// Scenario 1 (DB_CONNECTION_POOL_EXHAUSTION): serves the read inside a transaction that
    /// stays open for the length of the request.
    /// </summary>
    /// <remarks>
    /// The small pool alone is not enough to exhaust anything. A read that returns its
    /// connection after a few milliseconds lets twenty connections serve any amount of load, so
    /// the pool size would drop from 200 to 20 with no visible effect whatsoever. Exhaustion
    /// needs connections to be *held*, and the ordinary way an application holds one for the
    /// length of a request is a per-request transaction — which is exactly how production pools
    /// are exhausted.
    ///
    /// The result is the signature in the catalogue: requests queue waiting to acquire a
    /// connection, latency climbs to the acquire timeout, and the failures are
    /// <c>NpgsqlException: The connection pool has been exhausted</c> rather than query errors.
    /// In the trace the time is spent *before* the database span, which is what separates this
    /// from a slow query.
    /// </remarks>
    public async Task<List<Order>> ListHoldingConnectionAsync(
        OrdersDbContext db,
        int limit,
        CancellationToken cancellationToken)
    {
        await using var transaction = await db.Database.BeginTransactionAsync(cancellationToken);

        var orders = await db.Orders
            .AsNoTracking()
            .OrderByDescending(o => o.CreatedAt)
            .Take(limit)
            .ToListAsync(cancellationToken);

        // Stands in for the rest of the unit of work — further reads, a write, a downstream
        // call — all of which would run on this same held connection.
        await Task.Delay(
            _chaos.GetInt(ChaosCodes.DbConnectionPoolExhaustion, "hold_ms", 800),
            cancellationToken);

        await transaction.CommitAsync(cancellationToken);

        return orders;
    }

    /// <summary>
    /// Scenario 4 (DB_N_PLUS_ONE_QUERY): loads an order's items one query at a time instead of
    /// with the eager-loading Include the healthy path uses.
    /// </summary>
    /// <remarks>
    /// Each query is trivially fast, so latency rises only moderately and nothing is logged. The
    /// signal that separates this from scenario 2 is structural and only visible in a trace:
    /// fifty sibling database spans rather than one slow one, and a trivial statement with a
    /// huge call count in pg_stat_statements.
    /// </remarks>
    public async Task<Order?> GetWithNPlusOneAsync(
        OrdersDbContext db,
        Guid id,
        CancellationToken cancellationToken)
    {
        var order = await db.Orders
            .AsNoTracking()
            .FirstOrDefaultAsync(o => o.Id == id, cancellationToken);

        if (order is null)
        {
            return null;
        }

        var itemIds = await db.OrderItems
            .AsNoTracking()
            .Where(i => i.OrderId == id)
            .Select(i => i.Id)
            .ToListAsync(cancellationToken);

        foreach (var itemId in itemIds)
        {
            var item = await db.OrderItems
                .AsNoTracking()
                .FirstOrDefaultAsync(i => i.Id == itemId, cancellationToken);

            if (item is not null)
            {
                order.Items.Add(item);
            }
        }

        return order;
    }

    public bool IsEnabled(string code) => _chaos.IsEnabled(code);

    /// <summary>
    /// Pads the orders table so a sequential scan over it actually costs something.
    /// </summary>
    /// <remarks>
    /// Done in one <c>generate_series</c> insert rather than through EF: two million round trips
    /// would take hours, and the rows themselves are scenery rather than domain data. The values
    /// are derived from the series rather than randomised because gen_random_uuid() and md5()
    /// per row dominate the insert cost at this size. The padding is
    /// left behind when the scenario is disabled, which is intended — a large table is a
    /// property of the database, not of the fault, and the healthy path is measured against the
    /// same table.
    /// </remarks>
    private async Task EnsurePaddingAsync(OrdersDbContext db, CancellationToken cancellationToken)
    {
        if (_padded)
        {
            return;
        }

        await _paddingLock.WaitAsync(cancellationToken);

        try
        {
            if (_padded)
            {
                return;
            }

            var target = _chaos.GetInt(ChaosCodes.DbSlowQueryMissingIndex, "row_count", 2_000_000);
            var current = await db.Orders.CountAsync(cancellationToken);
            var missing = target - current;

            if (missing > 0)
            {
                _logger.LogWarning(
                    "Padding svc_orders.orders with {Count} rows for {ChaosCode}",
                    missing,
                    ChaosCodes.DbSlowQueryMissingIndex);

                await db.Database.ExecuteSqlAsync(
                    $"""
                     INSERT INTO svc_orders.orders
                         ("Id", "UserId", "Status", "TotalAmount", "Currency",
                          "PaymentReference", "CreatedAt")
                     SELECT ('11111111-0000-0000-0000-' || lpad(g::text, 12, '0'))::uuid,
                            '00000000-0000-0000-0000-0000000000ff'::uuid,
                            'Paid',
                            round((g % 100000)::numeric / 100, 2),
                            'TRY',
                            'PAY-PAD-' || lpad(g::text, 12, '0'),
                            now() - (g % 100000 || ' minutes')::interval
                     FROM generate_series(1, {missing}) g
                     """,
                    cancellationToken);
            }

            _padded = true;
        }
        finally
        {
            _paddingLock.Release();
        }
    }
}
