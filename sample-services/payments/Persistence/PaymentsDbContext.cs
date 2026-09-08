using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Payments.Domain;

namespace Sentinel.Samples.Payments.Persistence;

public sealed class PaymentsDbContext : DbContext
{
    public const string Schema = "svc_payments";

    public PaymentsDbContext(DbContextOptions<PaymentsDbContext> options) : base(options)
    {
    }

    public DbSet<Payment> Payments => Set<Payment>();

    public DbSet<MerchantBalance> MerchantBalances => Set<MerchantBalance>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.HasDefaultSchema(Schema);

        modelBuilder.Entity<Payment>(entity =>
        {
            entity.ToTable("payments");
            entity.HasKey(p => p.Id);

            entity.Property(p => p.Amount).HasPrecision(12, 2);
            entity.Property(p => p.Currency).HasMaxLength(3).IsRequired();
            entity.Property(p => p.Status).HasConversion<string>().HasMaxLength(32);
            entity.Property(p => p.ProviderReference).HasMaxLength(64).IsRequired();

            entity.HasIndex(p => p.OrderId);
            entity.HasIndex(p => p.CreatedAt);
        });

        modelBuilder.Entity<MerchantBalance>(entity =>
        {
            entity.ToTable("merchant_balances");
            entity.HasKey(b => b.Id);

            entity.Property(b => b.MerchantCode).HasMaxLength(32).IsRequired();
            entity.Property(b => b.Balance).HasPrecision(14, 2);

            entity.HasIndex(b => b.MerchantCode).IsUnique();
        });
    }
}
