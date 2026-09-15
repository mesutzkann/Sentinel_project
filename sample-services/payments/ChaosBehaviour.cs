using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Payments.Persistence;

namespace Sentinel.Samples.Payments;

/// <summary>
/// Runtime behaviour behind the payments scenarios that need more than a flipped branch.
/// </summary>
public sealed class ChaosBehaviour : IChaosActivationHandler
{
    private const string MerchantA = "MERCHANT-A";
    private const string MerchantB = "MERCHANT-B";

    /// <summary>
    /// Long enough for the opposite-order transaction to take its first lock, so the two
    /// genuinely interleave. Shorter and they would usually serialise and never deadlock.
    /// </summary>
    private static readonly TimeSpan InterleaveDelay = TimeSpan.FromMilliseconds(150);

    private readonly ChaosRegistry _chaos;
    private readonly IServiceScopeFactory _scopeFactory;
    private readonly ILogger<ChaosBehaviour> _logger;

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
    /// Scenario 14 is the only one here that needs an activation hook, and what it needs is a
    /// timestamp. The scenario is a *deployment*, not a defect that was always there: the
    /// evidence the agent has to find is that the error rate stepped up at one instant and that
    /// something was rolled out at that same instant. The commit already exists in the repository
    /// and its own timestamp is whenever it was authored, so the line below is what ties it to
    /// now.
    ///
    /// Logged at warning, and worded the way a deployment tool words it, because this line is
    /// meant to be found by <c>search_logs</c> and then taken to git-mcp.
    /// </remarks>
    public Task OnEnabledAsync(string code, CancellationToken cancellationToken)
    {
        if (code == ChaosCodes.BadDeploymentRegression)
        {
            var commit = _chaos.GetString(
                ChaosCodes.BadDeploymentRegression, "commit", "unknown");

            _logger.LogWarning(
                "Deployment of commit {Commit} to payments completed at {DeployedAt:O}",
                commit,
                DateTimeOffset.UtcNow);
        }

        return Task.CompletedTask;
    }

    /// <summary>
    /// Scenario 11 (DOWNSTREAM_LATENCY_CASCADE): holds the authorisation open for seconds.
    /// </summary>
    /// <remarks>
    /// A wait rather than work, and the distinction is the scenario's discriminator against
    /// scenario 15: the thread is idle, container CPU does not move, and the time shows up as a
    /// span that is doing nothing. What makes this one interesting is not the delay but what it
    /// does to everyone else — orders awaits payments and gateway awaits orders, so p99 rises in
    /// three services at once while only one of them is the cause.
    ///
    /// **Nothing is logged here on purpose.** The catalogue's signature is that payments is quiet
    /// while timeouts appear at the edge, and a warning in the slow service would hand the agent
    /// the answer that the trace is supposed to make it work for.
    /// </remarks>
    public Task DelayAuthorizationAsync(CancellationToken cancellationToken) =>
        Task.Delay(
            _chaos.GetInt(ChaosCodes.DownstreamLatencyCascade, "delay_ms", 3000),
            cancellationToken);

    /// <summary>
    /// Scenario 3 (DB_DEADLOCK): takes the two merchant balance locks in one order while a
    /// concurrent transaction takes them in the other.
    /// </summary>
    /// <remarks>
    /// A real deadlock, resolved by PostgreSQL rather than simulated: whichever transaction it
    /// picks as the victim dies with SQLSTATE 40P01 and the other commits. Which one loses is
    /// not deterministic, which is exactly the point — the scenario is recognised by errors that
    /// spike and recover on their own rather than by a sustained failure, and
    /// <c>deadlock_timeout=1s</c> in the compose file is what bounds the spike.
    ///
    /// The caller's own transaction is one of the two contenders, so roughly half the time the
    /// request itself is the victim and returns 500. The other half it commits and the
    /// background contender logs the deadlock instead.
    /// </remarks>
    public async Task ContendForBalancesAsync(
        PaymentsDbContext db,
        CancellationToken cancellationToken)
    {
        var opposite = Task.Run(() => RunOppositeOrderAsync(cancellationToken), cancellationToken);

        await using var transaction = await db.Database.BeginTransactionAsync(cancellationToken);

        await TouchBalanceAsync(db, MerchantA, cancellationToken);
        await Task.Delay(InterleaveDelay, cancellationToken);
        await TouchBalanceAsync(db, MerchantB, cancellationToken);

        await transaction.CommitAsync(cancellationToken);

        // Awaited rather than left running: an unobserved deadlock would be swallowed, and the
        // 40P01 log line is the primary evidence for this scenario.
        await opposite;
    }

    /// <summary>The mirror image: B then A, on its own connection.</summary>
    private async Task RunOppositeOrderAsync(CancellationToken cancellationToken)
    {
        await using var scope = _scopeFactory.CreateAsyncScope();
        var db = scope.ServiceProvider.GetRequiredService<PaymentsDbContext>();

        try
        {
            await using var transaction =
                await db.Database.BeginTransactionAsync(cancellationToken);

            await TouchBalanceAsync(db, MerchantB, cancellationToken);
            await Task.Delay(InterleaveDelay, cancellationToken);
            await TouchBalanceAsync(db, MerchantA, cancellationToken);

            await transaction.CommitAsync(cancellationToken);
        }
        catch (Exception ex)
        {
            // Swallowed on purpose: this transaction exists only to contend. When PostgreSQL
            // picks it as the victim the request that triggered it succeeds, and the deadlock
            // is still on the record here.
            _logger.LogError(ex, "Balance update lost a deadlock");
        }
    }

    private static Task TouchBalanceAsync(
        PaymentsDbContext db,
        string merchantCode,
        CancellationToken cancellationToken) =>
        db.Database.ExecuteSqlAsync(
            $"""
             UPDATE svc_payments.merchant_balances
             SET "Balance" = "Balance" + 1, "UpdatedAt" = now()
             WHERE "MerchantCode" = {merchantCode}
             """,
            cancellationToken);
}
