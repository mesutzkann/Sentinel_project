using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Payments.Persistence;

namespace Sentinel.Samples.Payments;

/// <summary>
/// Runtime behaviour behind the payments scenarios that need more than a flipped branch.
/// </summary>
public sealed class ChaosBehaviour
{
    private const string MerchantA = "MERCHANT-A";
    private const string MerchantB = "MERCHANT-B";

    /// <summary>
    /// Long enough for the opposite-order transaction to take its first lock, so the two
    /// genuinely interleave. Shorter and they would usually serialise and never deadlock.
    /// </summary>
    private static readonly TimeSpan InterleaveDelay = TimeSpan.FromMilliseconds(150);

    private readonly IServiceScopeFactory _scopeFactory;
    private readonly ILogger<ChaosBehaviour> _logger;

    public ChaosBehaviour(IServiceScopeFactory scopeFactory, ILogger<ChaosBehaviour> logger)
    {
        _scopeFactory = scopeFactory;
        _logger = logger;
    }

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
