using Microsoft.EntityFrameworkCore;
using Sentinel.Samples.Orders.Domain;

namespace Sentinel.Samples.Orders.Persistence;

public sealed class OrdersDbContext : DbContext
{
    public const string Schema = "svc_orders";

    public OrdersDbContext(DbContextOptions<OrdersDbContext> options) : base(options)
    {
    }

    public DbSet<Order> Orders => Set<Order>();

    public DbSet<OrderItem> OrderItems => Set<OrderItem>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.HasDefaultSchema(Schema);

        modelBuilder.Entity<Order>(entity =>
        {
            entity.ToTable("orders");
            entity.HasKey(o => o.Id);

            entity.Property(o => o.Status).HasConversion<string>().HasMaxLength(32);
            entity.Property(o => o.TotalAmount).HasPrecision(12, 2);
            entity.Property(o => o.Currency).HasMaxLength(3);
            entity.Property(o => o.PaymentReference).HasMaxLength(64);

            entity.HasMany(o => o.Items)
                .WithOne()
                .HasForeignKey(i => i.OrderId)
                .OnDelete(DeleteBehavior.Cascade);

            // Chaos scenario 2 (DB_SLOW_QUERY_MISSING_INDEX) works by querying a column that has
            // no index. These two exist so the *healthy* path is genuinely fast — otherwise the
            // scenario would not be distinguishable from normal behaviour.
            entity.HasIndex(o => o.UserId);
            entity.HasIndex(o => o.CreatedAt);
        });

        modelBuilder.Entity<OrderItem>(entity =>
        {
            entity.ToTable("order_items");
            entity.HasKey(i => i.Id);

            entity.Property(i => i.ProductName).HasMaxLength(200).IsRequired();
            entity.Property(i => i.UnitPrice).HasPrecision(12, 2);

            entity.Ignore(i => i.LineTotal);

            entity.HasIndex(i => i.OrderId);
        });
    }
}
